"""Scripted interactions during capture."""

from __future__ import annotations

from dataclasses import dataclass

from playwright.async_api import Error as PlaywrightError

STEP_TIMEOUT_MS = 5000


@dataclass
class Step:
    action: str  # click | type | press | wait | scroll
    selector: str | None = None
    text: str | None = None
    key: str | None = None
    ms: int | None = None


def parse_steps(spec: str | list[Step] | None) -> list[Step]:
    if not spec:
        return []
    if isinstance(spec, list):
        return spec

    steps: list[Step] = []
    for raw in spec.split(";"):
        part = raw.strip()
        if not part:
            continue
        verb, _, rest = part.partition(" ")
        verb = verb.lower()
        rest = rest.strip()

        if verb == "click":
            if not rest:
                raise ValueError("click requires a selector")
            steps.append(Step("click", selector=rest))
        elif verb == "type":
            selector, eq, text = rest.partition("=")
            if not eq:
                raise ValueError("type requires '<selector>=<text>'")
            steps.append(Step("type", selector=selector.strip(), text=text))
        elif verb == "press":
            if not rest:
                raise ValueError("press requires a key")
            steps.append(Step("press", key=rest))
        elif verb == "wait":
            try:
                ms = int(rest)
            except ValueError:
                raise ValueError(f"wait requires milliseconds, got {rest!r}")
            steps.append(Step("wait", ms=ms))
        elif verb == "scroll":
            steps.append(Step("scroll"))
        else:
            raise ValueError(f"unknown step: {verb!r}")
    return steps


async def run_steps(page, steps: list[Step]) -> None:
    for step in steps:
        try:
            if step.action == "click":
                await page.click(step.selector, timeout=STEP_TIMEOUT_MS)
            elif step.action == "type":
                await page.fill(step.selector, step.text or "", timeout=STEP_TIMEOUT_MS)
            elif step.action == "press":
                await page.keyboard.press(step.key)
            elif step.action == "wait":
                await page.wait_for_timeout(step.ms or 0)
            elif step.action == "scroll":
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
        except PlaywrightError:
            pass
