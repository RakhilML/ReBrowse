"""rebrowse command-line interface."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import click

from rebrowse.config import ensure_dirs

if TYPE_CHECKING:
    from collections.abc import Callable

    from rebrowse.models import CaptureResult

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _emit(result: dict, error_code: int = 1) -> None:
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    if result.get("error") and not result.get("success"):
        sys.exit(error_code)


@click.group()
def main():
    """rebrowse — learn a website's internal APIs, then call them directly.

    \b
      build <url>       capture a site and save its APIs as a skill
      import-har <har>  build a skill from a HAR export (DevTools, proxy, e2e run)
      run <prompt>      pick a saved API for a request and call it
      verify <target>   health-check a skill's read endpoints
      openapi <target>  export a skill or a recording as an OpenAPI 3.1 document
      mock <source>     serve recorded API responses on localhost
      diff <a> <b>      report API changes between two recordings
      contract <source> replay recorded reads against a server, report breaking changes
      baseline <source> write a recording that is safe to commit (no credentials or response values)
      mcp               serve skills to agents over MCP (stdio)
    """
    ensure_dirs()


@main.command()
@click.argument("url")
@click.option("--steps", "-s", default=None,
              help='Interactions that trigger more APIs, e.g. "type #q=cats; click #go".')
def build(url: str, steps: str | None):
    """Capture a website and reverse-engineer its APIs into a skill."""
    from rebrowse.capture.steps import parse_steps
    from rebrowse.orchestrator.pipeline import build as do_build

    try:
        parse_steps(steps)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="--steps")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    _emit(asyncio.run(do_build(url, steps=steps)))


@main.command("import-har", short_help="Build a skill from a HAR export, without a browser.")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--domain", "-d", default=None, metavar="HOST[:PORT]",
              help="Site in the HAR to learn (default: host of the first HTML page).")
def import_har(file: Path, domain: str | None):
    """Build a skill from a HAR file exported by browser DevTools, a proxy or a test run.

    No request is replayed. Cookies, Authorization and other credential headers are dropped,
    and secret-named query and body values are redacted before anything is stored.
    """
    from rebrowse.orchestrator.pipeline import import_har as do_import

    _emit(asyncio.run(do_import(file, domain)))


@main.command()
@click.argument("prompt")
@click.option("--dry-run", "-n", is_flag=True, help="Resolve the plan without sending anything.")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Confirm write/destructive calls.")
def run(prompt: str, dry_run: bool, assume_yes: bool):
    """Pick the saved API that matches PROMPT and call it.

    Read-only calls run directly; calls that change state need --yes.
    """
    from rebrowse.orchestrator.pipeline import run as do_run

    _emit(asyncio.run(do_run(prompt, dry_run=dry_run, assume_yes=assume_yes)))


@main.command()
@click.argument("target")
def verify(target: str):
    """Re-check a skill's read endpoints and record which still work.

    TARGET is a skill id or domain. Writes are never executed. Requests are paced per
    host (REBROWSE_HOST_INTERVAL seconds, default 1).
    """
    from rebrowse.orchestrator.pipeline import verify as do_verify

    _emit(asyncio.run(do_verify(target)))


def _recording_document(source: str, domain: str | None) -> tuple[dict, dict]:
    """Document the recording file SOURCE; raises ValueError."""
    from rebrowse.openapi import recording_to_openapi

    path, capture = _traffic(source, domain)
    document = recording_to_openapi(capture)
    if not document["paths"]:
        raise ValueError(f"No API traffic for {capture.domain} in {path}")
    return document, {"source": str(path.resolve()), "domain": capture.domain}


def _skill_document(target: str) -> tuple[dict, dict]:
    """Export the skill TARGET names; raises ValueError."""
    from rebrowse.openapi import skill_to_openapi
    from rebrowse.store.skills import resolve_skill

    skill = resolve_skill(target)
    if skill is None:
        raise ValueError(f"No skill matching '{target}'.")
    return skill_to_openapi(skill), {"skill_id": skill.skill_id, "domain": skill.domain}


@main.command()
@click.argument("target")
@click.option("--domain", "-d", default=None, metavar="HOST[:PORT]",
              help="Site in a HAR to document (default: host of the first HTML page).")
@click.option("--out", "-o", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Write the document to FILE instead of stdout.")
def openapi(target: str, domain: str | None, out: Path | None):
    """Export a skill or a recording as an OpenAPI 3.1 document (JSON).

    TARGET is a HAR file, a capture saved by build or a baseline, documented from its
    traffic with no LLM or skill, so a HAR and the baseline written from it give the same
    bytes. Any other TARGET is a skill id or domain. Nothing is sent over the network, and
    sensitive values in bodies, query strings and headers are redacted.
    """
    recording = Path(target).is_file()
    if domain and not recording:
        raise click.UsageError("--domain needs a recording file as TARGET")
    if not recording and ("/" in target or "\\" in target or target.endswith((".har", ".json"))):
        _emit({"error": f"No such file: {target}"})
        return
    try:
        if recording:
            document, summary = _recording_document(target, domain)
        else:
            document, summary = _skill_document(target)
    except ValueError as e:
        _emit({"error": str(e)})
        return
    data = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8", errors="backslashreplace")
    if out is None:
        click.get_binary_stream("stdout").write(data)
        return
    try:
        out.write_bytes(data)
    except OSError as e:
        _emit({"error": f"Could not write {out}: {e}"})
        return
    _emit({
        **summary,
        "path": str(out.resolve()),
        "paths": len(document["paths"]),
        "operations": sum(len(methods) for methods in document["paths"].values()),
    })


def _port_taken(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _netloc(text: str) -> str:
    return urlsplit(text).netloc if "://" in text else text


def _traffic(source: str, domain: str | None) -> tuple[Path, CaptureResult]:
    """Load SOURCE as a file, else the newest saved capture of that host; raises ValueError."""
    from rebrowse.capture.store import latest_capture, load_traffic, saved_domains

    path: Path | None = Path(source)
    if not path.is_file():
        path = latest_capture(_netloc(source))
    if path is None:
        saved = ", ".join(saved_domains()) or "none"
        raise ValueError(f"'{source}' is neither a file nor a domain with a saved capture "
                         f"(saved: {saved}). For an imported HAR, pass the HAR file.")
    return path, load_traffic(path, _netloc(domain) if domain else None)


@main.command(short_help="Serve recorded API responses on 127.0.0.1.")
@click.argument("source")
@click.option("--domain", "-d", default=None, metavar="HOST[:PORT]",
              help="Site in a HAR to mock (default: host of the first HTML page).")
@click.option("--port", "-p", type=click.IntRange(0, 65535), default=8787, show_default=True,
              help="Port on 127.0.0.1; 0 picks a free one.")
def mock(source: str, domain: str | None, port: int):
    """Answer a frontend's API calls from recorded traffic, on 127.0.0.1 only.

    SOURCE is a HAR file, a capture saved by build, or a HOST[:PORT] whose newest saved
    capture is used. Requests are matched on templated paths and GraphQL operations.
    Nothing is forwarded to the real site, secret-named values in responses are redacted,
    and no recorded header but content-type is sent back.
    """
    from rebrowse.mock import MockServer, build_routes, route_table

    try:
        path, capture = _traffic(source, domain)
    except ValueError as e:
        _emit({"error": str(e)})
        return
    routes = build_routes(capture)
    if not routes:
        _emit({"error": f"No API responses for {capture.domain} in {path}"})
        return
    try:
        if port and _port_taken(port):
            raise OSError("port is already in use")
        server = MockServer(routes, port)
    except OSError as e:
        _emit({"error": f"Cannot listen on 127.0.0.1:{port}: {e}"})
        return
    _emit({"url": server.url, "domain": capture.domain, "source": str(path.resolve()),
           "routes": route_table(routes)})
    sys.stdout.flush()
    with server, contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


def _side(path: Path, capture: CaptureResult, facts: dict) -> dict:
    bodies = sum(1 for fact in facts.values() if fact.shape.seen)
    return {"source": str(path.resolve()), "domain": capture.domain, "routes": len(facts),
            "bodies": bodies}


def _accepted_options(command: Callable) -> Callable:
    command = click.option(
        "--update-accepted", is_flag=True,
        help="Rewrite the --accepted FILE from this run: keep entries that still match or "
             "that it could not check, drop stale ones, add the breaking changes it does not "
             "accept yet.")(command)
    return click.option(
        "--accepted", type=click.Path(dir_okay=False, path_type=Path), default=None,
        metavar="FILE",
        help='JSON list of reviewed breaking changes; matching ones are reported as '
             '"accepted" and do not fail the run.')(command)


def _accepted_entries(file: Path | None, update: bool) -> list[dict] | None:
    """Load the --accepted FILE; raises ValueError when it is invalid."""
    from rebrowse.accepted import load_accepted

    if file is None:
        if update:
            raise click.UsageError("--update-accepted needs --accepted FILE")
        return None
    return load_accepted(file, missing_ok=update)


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _settle_accepted(command: str, report: dict, file: Path | None,
                     entries: list[dict] | None, update: bool, uncompared: set[str]) -> None:
    """Mark the changes FILE accepts and recount breaking, rewriting FILE first on update."""
    from rebrowse.accepted import (
        ACCEPTED,
        apply_accepted,
        needs_review,
        unsettled,
        update_accepted,
        write_accepted,
    )
    from rebrowse.drift import BREAKING

    if file is None or entries is None:
        return
    changes = report.pop("changes")
    failed = {change["route"] for change in changes if unsettled(change)}
    if update and failed:
        click.echo(f"[{command}] not updating {file}: {_count(len(failed), 'route', 'routes')} "
                   "failed to replay or answered with a server error", err=True)
    elif update:
        entries = update_accepted(changes, entries, uncompared)
        if held := sum(needs_review(change) for change in changes):
            click.echo(f"[{command}] not accepting {_count(held, 'change', 'changes')} from an "
                       f"answer to a sign-in redirect, 401 or 403 in {file}; add them by hand "
                       "if they are intended", err=True)
        try:
            write_accepted(file, entries)
        except OSError as e:
            _emit({"error": f"Could not write {file}: {e}"}, error_code=2)
            return
    changes, stale, unchecked = apply_accepted(changes, entries, uncompared)
    report.update(breaking=sum(change["severity"] == BREAKING for change in changes),
                  accepted=sum(change["severity"] == ACCEPTED for change in changes),
                  stale=stale, unchecked=unchecked, changes=changes)
    if stale:
        hint = "" if update else " or rerun with --update-accepted"
        click.echo(f"[{command}] {_count(len(stale), 'entry', 'entries')} in {file} matched no "
                   f"breaking change; remove stale entries{hint}", err=True)


@main.command(short_help="Report API changes between two recordings.")
@click.argument("base")
@click.argument("head")
@click.option("--domain", "-d", "domains", multiple=True, metavar="HOST[:PORT]",
              help="Site to compare in HAR files; give it twice for BASE then HEAD "
                   "(default: host of each one's first HTML page).")
@_accepted_options
def diff(base: str, head: str, domains: tuple[str, ...], accepted: Path | None,
         update_accepted: bool):
    """Report the API changes from BASE to HEAD that can break their client.

    Each is a HAR file, a capture saved by build, or a HOST[:PORT] whose newest saved
    capture is used. Nothing is sent over the network, and the report holds routes,
    statuses, media types, field paths and JSON types, never a response value (object keys
    that read like names do appear, as field names). Breaking changes listed in the
    --accepted FILE are reported as accepted. Exits 1 when a change is breaking and not
    accepted, and 2 when an input or FILE cannot be read or an input holds no API traffic.
    """
    from rebrowse.drift import BREAKING, compare_facts, route_facts

    if len(domains) > 2:
        raise click.BadParameter("give it at most twice", param_hint="--domain")
    base_domain, head_domain = (domains * 2)[:2] if domains else (None, None)
    try:
        entries = _accepted_entries(accepted, update_accepted)
        base_path, base_capture = _traffic(base, base_domain)
        head_path, head_capture = _traffic(head, head_domain)
    except ValueError as e:
        _emit({"error": str(e)}, error_code=2)
        return
    before, after = route_facts(base_capture), route_facts(head_capture)
    for path, capture, facts in ((base_path, base_capture, before),
                                 (head_path, head_capture, after)):
        if not facts:
            _emit({"error": f"No API traffic for {capture.domain} in {path}"}, error_code=2)
            return
    changes = compare_facts(before, after)
    report = {
        "base": _side(base_path, base_capture, before),
        "head": _side(head_path, head_capture, after),
        "breaking": sum(change["severity"] == BREAKING for change in changes),
        "changes": changes,
    }
    if accepted:
        either = before | after
        one_sided = {either[key].route.name for key in before.keys() ^ after.keys()}
        _settle_accepted("diff", report, accepted, entries, update_accepted, one_sided)
    _emit(report)
    sys.exit(1 if report["breaking"] else 0)


@main.command(short_help="Replay recorded reads against a server.")
@click.argument("source")
@click.option("--against", required=True, metavar="ORIGIN",
              help="Server to test, as scheme://host[:port].")
@click.option("--header-env", "header_env", multiple=True, metavar="NAME=ENVVAR",
              help="Send header NAME, with the value of environment variable ENVVAR, on every "
                   "replay to ORIGIN. Repeatable. Values are read from the environment only "
                   "and are never printed. Replaces a recorded header of the same name and the "
                   "header an 'auth set' key for ORIGIN adds. Host, User-Agent and framing "
                   "headers cannot be set.")
@click.option("--domain", "-d", default=None, metavar="HOST[:PORT]",
              help="Site to test in a HAR (default: host of the first HTML page).")
@_accepted_options
def contract(source: str, against: str, header_env: tuple[str, ...], domain: str | None,
             accepted: Path | None, update_accepted: bool):
    """Replay the reads recorded in SOURCE against ORIGIN and report what breaks the client.

    SOURCE is a HAR file, a capture saved by build, or a HOST[:PORT] whose newest saved
    capture is used. Only reads are sent, at most three per route, one at a time, without
    retries or following redirects; calls to sibling hosts are skipped. Secret-named values
    are redacted and recorded credentials and cookies are never sent. The credentials sent
    to ORIGIN are the --header-env headers and an API key stored with 'auth set' for
    ORIGIN's host; rebrowse never signs in itself. Answers are judged as diff judges them,
    and breaking changes listed in the --accepted FILE are reported as accepted; a failed
    replay cannot be accepted. Exits 1 when a change is breaking and not accepted or a
    replay failed, and 2 on bad input or FILE, no reads to replay, when ORIGIN never
    answered, or when it refused or redirected every read the recording answered.
    """
    from rebrowse.contract import BLOCKED, NO_RESPONSE, env_headers, parse_target, run_contract

    try:
        target = parse_target(against)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="--against")
    try:
        headers = env_headers(header_env, os.environ)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="--header-env")
    try:
        entries = _accepted_entries(accepted, update_accepted)
        path, capture = _traffic(source, domain)
    except ValueError as e:
        _emit({"error": str(e)}, error_code=2)
        return
    variables = dict(spec.split("=", 1) for spec in header_env)
    report = asyncio.run(run_contract(capture, target, headers, variables))
    if "error" in report:
        _emit(report, error_code=2)
        return
    report = {"source": str(path.resolve()), **report}
    uncompared = {row["route"] for row in report["skipped"]} | {
        change["route"] for change in report["changes"]
        if change["kind"] in (NO_RESPONSE, BLOCKED)}
    _settle_accepted("contract", report, accepted, entries, update_accepted, uncompared)
    _emit(report)
    sys.exit(1 if report["breaking"] else 0)


@main.command(short_help="Write a recording that is safe to commit.")
@click.argument("source")
@click.option("--domain", "-d", default=None, metavar="HOST[:PORT]",
              help="Site in a HAR to keep (default: host of the first HTML page).")
@click.option("--out", "-o", type=click.Path(path_type=Path), default=None, metavar="FILE",
              help="Write the baseline to FILE instead of stdout.")
def baseline(source: str, domain: str | None, out: Path | None):
    """Write the API traffic in SOURCE as a recording that is safe to commit.

    SOURCE is a HAR file, a capture saved by build, or a HOST[:PORT] whose newest saved
    capture is used. The output is a capture that diff, contract and mock read as SOURCE.
    Credentials are removed as contract removes them before sending, every response value
    becomes a placeholder of the same JSON type, and requests are sorted and deduplicated,
    so the file is the same for the same API. Request paths, query values and the bodies of
    reads are kept, since contract replays them. Nothing is sent over the network.
    """
    from rebrowse.baseline import baseline_bytes, make_baseline, write_baseline
    from rebrowse.mock import build_routes

    try:
        path, capture = _traffic(source, domain)
    except ValueError as e:
        _emit({"error": str(e)})
        return
    result = make_baseline(capture)
    if not result.requests:
        _emit({"error": f"No API traffic for {capture.domain} in {path}"})
        return
    data = baseline_bytes(result)
    if out is None:
        click.get_binary_stream("stdout").write(data)
        return
    out = out.resolve()
    try:
        write_baseline(out, data)
    except OSError as e:
        _emit({"error": f"Could not write {out}: {e}"})
        return
    _emit({"source": str(path.resolve()), "domain": result.domain, "path": str(out),
           "routes": len(build_routes(result, redirects=True)),
           "recorded": len(capture.requests), "requests": len(result.requests)})


@main.command()
@click.option("--query", "-q", default=None, help="Semantic search query.")
def skills(query: str | None):
    """List or search saved skills."""
    from rebrowse.store.skills import list_all_skills, search_skills

    if query:
        for skill, score in search_skills(query, limit=10):
            click.echo(f"  [{score:.3f}] {skill.skill_id}  {skill.domain}  "
                       f"({len(skill.endpoints)} endpoints)")
        return
    all_skills = list_all_skills()
    if not all_skills:
        click.echo("No skills stored yet. Run 'build' on a URL first.")
    for s in all_skills:
        click.echo(f"  {s.skill_id}  {s.domain}  ({len(s.endpoints)} endpoints)  {s.updated_at}")


@main.command()
@click.argument("skill_id")
def show(skill_id: str):
    """Show a saved skill as JSON."""
    from rebrowse.store.skills import list_all_skills

    skill = next((s for s in list_all_skills() if s.skill_id == skill_id), None)
    if skill is None:
        raise click.ClickException(f"Skill {skill_id} not found.")
    click.echo(json.dumps(skill.model_dump(mode="json"), indent=2))


@main.command()
@click.argument("skill_id")
def delete(skill_id: str):
    """Delete a saved skill."""
    from rebrowse.store.skills import delete_skill

    if not delete_skill(skill_id):
        raise click.ClickException(f"Skill {skill_id} not found.")
    click.echo(f"Deleted {skill_id}")


@main.command()
def mcp():
    """Serve saved skills to agents over MCP (stdio).

    Tools: search_skills, list_operations, read (read-only), act (needs confirm=true).
    """
    from rebrowse.mcp_server import main as run_server

    run_server()


@main.group()
def auth():
    """Manage API keys for domains."""


@auth.command("set")
@click.argument("domain")
@click.argument("key")
@click.option("--type", "-t", "auth_type", type=click.Choice(["bearer", "header", "query"]),
              default="bearer", show_default=True,
              help="bearer: Authorization header; header: X-API-Key; query: ?api_key=")
def auth_set(domain: str, key: str, auth_type: str):
    """Store an API key, sent only to DOMAIN (and same-site hosts of skills built on it).

    \b
      python -m rebrowse auth set api.github.com ghp_xxx
      python -m rebrowse auth set maps.googleapis.com AIza... --type query
    """
    from rebrowse.auth.vault import VaultError, store_api_key

    try:
        store_api_key(domain, key, auth_type)
    except VaultError as e:
        raise click.ClickException(str(e))
    click.echo(f"Stored {auth_type} API key for {domain}")


@auth.command("remove")
@click.argument("domain")
def auth_remove(domain: str):
    """Remove a stored API key."""
    from rebrowse.auth.vault import VaultError, delete_api_key

    try:
        removed = delete_api_key(domain)
    except VaultError as e:
        raise click.ClickException(str(e))
    if not removed:
        raise click.ClickException(f"No API key found for {domain}")
    click.echo(f"Removed API key for {domain}")


@auth.command("list")
def auth_list():
    """List domains with stored API keys."""
    from rebrowse.auth.vault import list_api_keys

    domains = list_api_keys()
    if not domains:
        click.echo("No API keys stored.")
    for d in domains:
        click.echo(f"  {d}")


if __name__ == "__main__":
    main()
