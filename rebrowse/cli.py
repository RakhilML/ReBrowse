"""rebrowse command-line interface."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import click

from rebrowse.config import ensure_dirs

if TYPE_CHECKING:
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
      openapi <target>  export a skill as an OpenAPI 3.1 document
      mock <source>     serve recorded API responses on localhost
      diff <a> <b>      report API changes between two recordings
      contract <source> replay recorded reads against a server, report breaking changes
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


@main.command()
@click.argument("target")
@click.option("--out", "-o", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Write the document to FILE instead of stdout.")
def openapi(target: str, out: Path | None):
    """Export a skill as an OpenAPI 3.1 document (JSON).

    TARGET is a skill id or domain. Nothing is sent over the network, and sensitive
    values in bodies, query strings and headers are redacted.
    """
    from rebrowse.openapi import skill_to_openapi
    from rebrowse.store.skills import resolve_skill

    skill = resolve_skill(target)
    if skill is None:
        _emit({"error": f"No skill matching '{target}'."})
        return
    document = skill_to_openapi(skill)
    text = json.dumps(document, indent=2, ensure_ascii=False)
    if out is None:
        click.get_binary_stream("stdout").write((text + "\n").encode("utf-8"))
        return
    try:
        out.write_text(text + "\n", encoding="utf-8", newline="\n")
    except OSError as e:
        _emit({"error": f"Could not write {out}: {e}"})
        return
    _emit({
        "skill_id": skill.skill_id,
        "domain": skill.domain,
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


@main.command(short_help="Report API changes between two recordings.")
@click.argument("base")
@click.argument("head")
@click.option("--domain", "-d", "domains", multiple=True, metavar="HOST[:PORT]",
              help="Site to compare in HAR files; give it twice for BASE then HEAD "
                   "(default: host of each one's first HTML page).")
def diff(base: str, head: str, domains: tuple[str, ...]):
    """Report the API changes from BASE to HEAD that can break their client.

    Each is a HAR file, a capture saved by build, or a HOST[:PORT] whose newest saved
    capture is used. Nothing is sent over the network, and the report holds routes,
    statuses, media types, field paths and JSON types, never a response value (object keys
    that read like names do appear, as field names). Exits 1 when a change is breaking and
    2 when an input cannot be read or holds no API traffic.
    """
    from rebrowse.drift import BREAKING, compare_facts, route_facts

    if len(domains) > 2:
        raise click.BadParameter("give it at most twice", param_hint="--domain")
    base_domain, head_domain = (domains * 2)[:2] if domains else (None, None)
    try:
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
    breaking = sum(change["severity"] == BREAKING for change in changes)
    _emit({
        "base": _side(base_path, base_capture, before),
        "head": _side(head_path, head_capture, after),
        "breaking": breaking,
        "changes": changes,
    })
    sys.exit(1 if breaking else 0)


@main.command(short_help="Replay recorded reads against a server.")
@click.argument("source")
@click.option("--against", required=True, metavar="ORIGIN",
              help="Server to test, as scheme://host[:port].")
@click.option("--domain", "-d", default=None, metavar="HOST[:PORT]",
              help="Site to test in a HAR (default: host of the first HTML page).")
def contract(source: str, against: str, domain: str | None):
    """Replay the reads recorded in SOURCE against ORIGIN and report what breaks the client.

    SOURCE is a HAR file, a capture saved by build, or a HOST[:PORT] whose newest saved
    capture is used. Only reads are sent, at most three per route, one at a time, without
    retries or following redirects; calls to sibling hosts are skipped. Secret-named values
    are redacted, recorded credentials and cookies are never sent, and the only credential
    is an API key stored with 'auth set' for ORIGIN's host. Answers are judged as diff
    judges them. Exits 1 when a change is breaking or a replay failed, and 2 on bad input,
    no reads to replay, or when ORIGIN never answered.
    """
    from rebrowse.contract import parse_target, run_contract

    try:
        target = parse_target(against)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="--against")
    try:
        path, capture = _traffic(source, domain)
    except ValueError as e:
        _emit({"error": str(e)}, error_code=2)
        return
    report = asyncio.run(run_contract(capture, target))
    if "error" in report:
        _emit(report, error_code=2)
        return
    _emit({"source": str(path.resolve()), **report})
    sys.exit(1 if report["breaking"] else 0)


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
