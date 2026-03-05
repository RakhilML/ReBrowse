"""CLI entry point — two commands: build and run."""

from __future__ import annotations
import asyncio
import json
import sys
import os
import click
from rebrowse.config import ensure_dirs

# Fix Windows console encoding for unicode
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


@click.group()
def main():
    """rebrowse — Local API reverse-engineering for browsers.

    Two commands:

      build <url>     Capture a website, extract its APIs, save as a skill.

      run <prompt>    Search skills, pick the right API, execute it.
    """
    ensure_dirs()


@main.command()
@click.argument("url")
def build(url: str):
    """Capture a website and reverse-engineer its APIs into a skill.

    Examples:

      python -m rebrowse build https://github.com/trending

      python -m rebrowse build https://news.ycombinator.com
    """
    from rebrowse.orchestrator.pipeline import build as do_build

    if not url.startswith("http"):
        url = f"https://{url}"

    result = _run(do_build(url))
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))


@main.command()
@click.argument("prompt")
def run(prompt: str):
    """Search stored skills and execute the best matching API.

    Examples:

      python -m rebrowse run "get trending repos from github"

      python -m rebrowse run "show top stories on hacker news"
    """
    from rebrowse.orchestrator.pipeline import run as do_run

    result = _run(do_run(prompt))
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))


@main.command()
@click.option("--query", "-q", default=None, help="Semantic search query")
def skills(query: str):
    """List or search stored skills."""
    from rebrowse.store.skills import list_all_skills, search_skills

    if query:
        results = search_skills(query, limit=10)
        for skill, score in results:
            print(f"  [{score:.3f}] {skill.skill_id}  {skill.domain}  ({len(skill.endpoints)} endpoints)")
    else:
        all_skills = list_all_skills()
        if not all_skills:
            print("No skills stored yet. Run 'build' on a URL first.")
            return
        for s in all_skills:
            print(f"  {s.skill_id}  {s.domain}  ({len(s.endpoints)} endpoints)  {s.updated_at}")


@main.command()
@click.argument("skill_id")
def show(skill_id: str):
    """Show full details of a stored skill."""
    from rebrowse.store.skills import list_all_skills

    for s in list_all_skills():
        if s.skill_id == skill_id:
            print(json.dumps(s.model_dump(), indent=2, default=str))
            return
    print(f"Skill {skill_id} not found.", file=sys.stderr)


@main.command()
@click.argument("skill_id")
def delete(skill_id: str):
    """Delete a stored skill."""
    from rebrowse.store.skills import delete_skill

    if delete_skill(skill_id):
        print(f"Deleted {skill_id}")
    else:
        print(f"Skill {skill_id} not found.", file=sys.stderr)


@main.group()
def auth():
    """Manage API keys and credentials for domains."""
    pass


@auth.command("set")
@click.argument("domain")
@click.argument("key")
@click.option(
    "--type", "-t", "auth_type",
    type=click.Choice(["bearer", "header", "query"]),
    default="bearer",
    help="How to send the key: bearer (Authorization header), header (X-API-Key), or query (?api_key=...)",
)
def auth_set(domain: str, key: str, auth_type: str):
    """Store an API key for a domain.

    Examples:

      python -m rebrowse auth set api.openai.com sk-abc123

      python -m rebrowse auth set maps.googleapis.com AIza... --type query

      python -m rebrowse auth set api.github.com ghp_xxx --type header
    """
    from rebrowse.auth.vault import store_api_key

    store_api_key(domain, key, auth_type)
    print(f"Stored {auth_type} API key for {domain}")


@auth.command("remove")
@click.argument("domain")
def auth_remove(domain: str):
    """Remove a stored API key for a domain."""
    from rebrowse.auth.vault import delete_api_key

    if delete_api_key(domain):
        print(f"Removed API key for {domain}")
    else:
        print(f"No API key found for {domain}", file=sys.stderr)


@auth.command("list")
def auth_list():
    """List all domains with stored API keys."""
    from rebrowse.auth.vault import list_api_keys

    domains = list_api_keys()
    if not domains:
        print("No API keys stored.")
        return
    for d in domains:
        print(f"  {d}")


if __name__ == "__main__":
    main()
