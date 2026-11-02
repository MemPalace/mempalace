"""Run the clean-user browser + stock MCP release gate on throwaway instances.

    python -m scripts.commonplace_release --output /tmp/commonplace-release-report

Install scripts/requirements-release.txt and Playwright Chromium first. Never
point this journey at an existing service: it creates its own empty instances.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import secrets
import subprocess
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from scripts.commonplace_release_support import (
    Blocked,
    Report,
    checked_json,
    require,
    throwaway_instance,
)

CHECKS = (
    "email_signup",
    "default_board",
    "create_space",
    "agent_connection",
    "mcp_startup",
    "card_owner_decision",
    "memory_save_search",
    "rule_owner_acceptance",
    "missing_login_token",
    "logout_login",
    "second_user_isolation",
    "wing_scope",
    "synthetic_social_signup",
    "storage_limit",
    "voice_limit",
)
NO_SCREENSHOT_CHECKS = {
    "agent_connection",
    "mcp_startup",
    "wing_scope",
    "storage_limit",
    "voice_limit",
}
BASELINE_RULE = "Keep the release journey's exact words intact."
BASELINE_MEMORY = "Workspace decisions preferences current work: release recall canary."
PROPOSED_RULE = "Keep every release memory verbatim. Ask the owner before changing this rule."
SUMMARY = "Preserve release memories and ask before changes."


async def api(page, path, method="GET", body=None, expected=200):
    response = await page.context.request.fetch(
        page.url.split("/app", 1)[0] + path,
        method=method,
        data=json.dumps(body) if body is not None else None,
        headers={"Content-Type": "application/json"},
        timeout=15000,
    )
    # Inspect status before JSON: missing routes can be plain HTML.
    checked_json(response.status, None, method, path, expected)
    return await response.json()


async def email_login(page, instance, email, *, signup=False):
    await page.goto(instance.origin + "/app/")
    await page.wait_for_load_state("networkidle")
    field = page.get_by_role("textbox", name="Email", exact=True)
    if not await field.count():
        raise Blocked("/app/ has no email request/signup form")
    await field.fill(email)
    mail_position = len(instance.mail.messages)
    async with page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith("/app/auth/email")
    ) as sent:
        await field.locator("xpath=ancestor::form").get_by_role("button").click()
    response = await sent.value
    checked_json(response.status, None, "POST", "/app/auth/email", 202)
    require(
        (await page.context.request.get(instance.origin + "/api/me")).status == 401,
        "Requesting an email link must not create a session before verification",
    )
    # Captured mail is immediate transport; allow a bounded asynchronous FIFO wait.
    link = await asyncio.to_thread(
        instance.mail.login_link, email, instance.origin, timeout=15, after=mail_position
    )
    async with page.expect_response(lambda r: r.url == link) as verified:
        await page.goto(link)
    require((await verified.value).status == 200, "Email link must open a read-only confirmation")
    require(
        (await page.context.request.get(instance.origin + "/api/me")).status == 401,
        "Opening an email link must not sign in before confirmation",
    )
    async with page.expect_response(
        lambda r: (
            r.request.method == "POST" and r.url.split("?", 1)[0].endswith("/app/auth/email/verify")
        )
    ) as confirmed:
        await page.get_by_role("button", name="Sign in", exact=True).click()
    require((await confirmed.value).status == 303, "Explicit email confirmation must sign in")
    await page.wait_for_url(instance.origin + "/app/**")
    me = await api(page, "/api/me")
    require(me["email"] == email, "Email link signed in the wrong person")
    # A fresh signup, rather than a privileged owner fixture, establishes authority.
    require(bool(me.get("workspace")), "Fresh signup did not create a personal workspace")
    if signup:
        require(me.get("actor") == "user:" + me["user"], "Signup did not create a person session")
    await page.locator(".app").wait_for()
    return me


async def social_login(page, instance, email):
    await page.goto(instance.origin + "/app/auth/github")
    await page.get_by_role("textbox", name="Email", exact=True).fill(email)
    await page.get_by_role("button", name="Continue", exact=True).click()
    await page.wait_for_url(instance.origin + "/app/**")
    me = await api(page, "/api/me")
    require(me["email"] == email, "Synthetic provider signup did not establish the new user")
    return me


@asynccontextmanager
async def stock_client(origin, token):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with (
        streamablehttp_client(
            origin + "/mcp",
            headers={"Authorization": "Bearer " + token},
            timeout=15,
            sse_read_timeout=15,
        ) as (
            read,
            write,
            _session_id,
        ),
        ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session,
    ):
        initialized = await session.initialize()
        yield session, initialized


async def tool(session, name, arguments=None, *, error=False):
    result = await session.call_tool(name, arguments or {})
    require(result.isError is error, f"MCP {name}: expected isError={error}")
    text = "\n".join(part.text for part in result.content if part.type == "text")
    # Commonplace returns both plain text and JSON-encoded text tool content.
    try:
        value = json.loads(text)
    except ValueError:
        return text
    return value if isinstance(value, str) else json.dumps(value)


class Journey:
    def __init__(self, browser, instance, output, report):
        self.browser, self.instance, self.output, self.report = browser, instance, output, report
        self.page = None
        self.values = {}
        self.email = "release-" + secrets.token_hex(6) + "@example.test"

    def need(self, *keys):
        if any(key not in self.values for key in keys):
            raise Blocked("Requires successful " + ", ".join(keys))
        return [self.values[key] for key in keys]

    async def screenshot(self, name):
        filename = name + ".png"
        await self.page.screenshot(
            path=str(self.output / filename),
            full_page=True,
            mask=[self.page.locator('input[name="token"], [data-agent-secret], #agent-secret')],
            timeout=10000,
        )
        return filename

    async def check(self, name, action):
        row = self.report.results[name]
        if name in {"mcp_startup", "wing_scope", "import_capture_raw_readback"}:
            row.evidence_kind = "protocol assertions"
        try:
            detail = await asyncio.wait_for(action(), timeout=45)
            row.status, row.detail = "PASS", detail or "Verified"
        except Blocked as exc:
            row.status, row.detail = "BLOCKED", str(exc)
        except AssertionError as exc:
            row.status, row.detail = "FAIL", str(exc)
        except Exception as exc:
            # SDK/browser exceptions can contain bearer headers and login URLs.
            row.status, row.detail = "FAIL", f"{type(exc).__name__}; inspect screenshot"
        if (
            self.page is not None
            and not row.screenshot
            and (name not in NO_SCREENSHOT_CHECKS or row.status != "PASS")
        ):
            try:
                row.screenshot = await self.screenshot(name)
            except Exception:
                row.status = "FAIL"
                row.detail += "; screenshot unavailable"

    async def signup(self):
        self.values["signup"] = await email_login(self.page, self.instance, self.email, signup=True)
        return "New email identity and personal workspace created through a captured email link"

    async def default_board(self):
        self.need("signup")
        boards = (await api(self.page, "/api/boards"))["boards"]
        require(bool(boards), "Brand-new user has no default space")
        require(
            "#/board/" in self.page.url, "Fresh signup must open the default space in board view"
        )
        await self.page.locator('.col[data-status="todo"]').wait_for()
        return "Default space exists and opens as a board"

    async def create_space(self):
        self.need("signup")
        await self.page.goto(self.instance.origin + "/app/#/boards")
        await (
            self.page.get_by_role("main")
            .get_by_role("button", name="New space", exact=True)
            .click()
        )
        await self.page.get_by_label("Name", exact=True).fill("Release journey")
        async with self.page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/boards")
        ) as created:
            await self.page.get_by_role("button", name="Create", exact=True).click()
        response = await created.value
        checked_json(response.status, None, "POST", "/api/boards", 201)
        self.values["space"] = await response.json()
        await self.page.wait_for_url("**/#/board/**")
        await self.page.locator('.col[data-status="todo"]').wait_for()
        return "Created a space using the browser form"

    async def agent_connection(self):
        self.need("signup")
        result = await api(
            self.page,
            "/api/connections",
            "POST",
            {"name": "release-agent", "client": "generic"},
            201,
        )
        token = result.get("token", "")
        require(token.startswith("cmmn_"), "Connection creation must return its one-time token")
        self.values["token"] = token
        self.values["connection"] = result["connection"]
        listed = await api(self.page, "/api/connections")
        require(token not in json.dumps(listed), "Connection listing leaked the one-time secret")
        await self.page.goto(self.instance.origin + "/app/#/settings")
        control = self.page.locator(
            "[data-agent-id=" + json.dumps(result["connection"]["id"]) + "]"
        ).first
        await control.wait_for()
        row = control.locator("xpath=ancestor::div[contains(@class, 'setting-row')]")
        require(
            (await row.locator("span").inner_text()).splitlines()[0] == "release-agent",
            "Settings omitted the created agent name",
        )
        return "Ordinary person session created a named agent token; listing hides the secret"

    async def mcp_startup(self):
        (token,) = self.need("token")
        await api(
            self.page,
            "/api/rules",
            "POST",
            {"agent": "*", "text": BASELINE_RULE, "kind": "instruction"},
            201,
        )
        await api(
            self.page, "/api/memory", "POST", {"text": BASELINE_MEMORY, "theme": "inbox"}, 201
        )
        async with stock_client(self.instance.origin, token) as (session, initialized):
            require(
                BASELINE_RULE in (initialized.instructions or ""),
                "Initialize omitted effective rules",
            )
            startup = await tool(session, "session_start", {"query": "release recall canary"})
            require(BASELINE_RULE in startup, "Startup read omitted effective rules")
            require(BASELINE_MEMORY in startup, "Startup read omitted exact saved memory")
            await tool(session, "rules_get", {"effective": True})
        self.values["startup"] = True
        return "Stock MCP SDK initialize returns rules; prescribed startup read fetches rules and exact memory"

    async def card_owner_decision(self):
        token, space = self.need("token", "space")
        async with stock_client(self.instance.origin, token) as (session, _initialized):
            await tool(session, "rules_get", {"effective": True})
            added = await tool(
                session,
                "task_add",
                {
                    "board": space["slug"],
                    "title": "Release canary card",
                    "body": "Verify the synthetic release journey before proceeding.",
                },
            )
            match = re.search(r"#(\d+) added", added)
            require(match is not None, "Agent did not create a card")
            number = int(match.group(1))
            await tool(session, "task_take_next", {"task": number})
            claimed = await api(self.page, f"/api/cards/{number}")
            require(claimed["status"] == "doing", "Agent claim did not start work")
            require(claimed["assignee"] == "conn:release-agent", "Agent did not hold its own card")
            await tool(
                session,
                "task_update",
                {
                    "task": number,
                    "status": "review",
                    "question": "May the release canary continue to completion?",
                    "context": "The synthetic release checks passed and require the owner's explicit choice.",
                    "options": ["Continue the release", "Stop the release"],
                },
            )
            review = await api(self.page, f"/api/cards/{number}")
            require(
                review["status"] == "review" and bool(review["ask"]),
                "Review did not open a real owner decision",
            )
            await tool(session, "task_update", {"task": number, "status": "done"}, error=True)
            require(
                (await api(self.page, f"/api/cards/{number}"))["status"] == "review",
                "Agent bypassed the pending owner decision",
            )
        await self.page.goto(self.instance.origin + f"/app/#/card/{number}")
        panel = self.page.get_by_role("dialog", name="Card", exact=True)
        async with self.page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith(f"/api/cards/{number}/decide")
        ):
            await panel.get_by_role("button", name=re.compile("^Continue the release")).click()
        answered = await api(self.page, f"/api/cards/{number}")
        require(
            answered["status"] == "todo" and not answered["ask"], "Owner's choice was not applied"
        )
        async with stock_client(self.instance.origin, token) as (session, _initialized):
            await tool(session, "rules_get", {"effective": True})
            await tool(session, "task_take_next", {"task": number})
            await tool(
                session,
                "task_update",
                {
                    "task": number,
                    "status": "done",
                    "lesson": "Completed after the owner's recorded approval.",
                },
            )
        completed = await api(self.page, f"/api/cards/{number}")
        require(
            completed["status"] == "done" and not completed["ask"],
            "Completed card must be done with no pending owner decision",
        )
        self.values["card"] = number
        await self.page.reload()
        panel = self.page.get_by_role("dialog", name="Card", exact=True)
        await panel.locator(".state.done").first.wait_for()
        require(
            await panel.locator('[data-action="decide"]').count() == 0,
            "Completed card still offers a live owner decision",
        )
        return (
            "Completed after owner approval; self-approval was refused. "
            "The earlier question and options remain as history; choices are disabled in the DOM, "
            "with the existing full-opacity styling unchanged."
        )

    async def memory_save_search(self):
        (token,) = self.need("token")
        text = "Release memory: café, two  spaces, and\na second line — exact words."
        async with stock_client(self.instance.origin, token) as (session, _initialized):
            await tool(session, "rules_get", {"effective": True})
            await tool(session, "memory_save", {"text": text})
            found = await tool(session, "memory_search", {"query": "café release memory"})
            require(
                json.dumps(text, ensure_ascii=False) in found,
                "Memory search did not preserve exact words",
            )
        matches = (await api(self.page, "/api/memory/search?q=release%20memory"))["results"]
        memory = next((row for row in matches if row["text"] == text), None)
        require(memory is not None, "Person cannot retrieve the agent's saved memory")
        self.values["memory"] = memory
        await self.page.goto(self.instance.origin + "/app/#/memory")
        await self.page.get_by_role("searchbox", name="Search memories").fill("café release memory")
        await self.page.get_by_role("button", name="Search", exact=True).click()
        await self.page.get_by_text(text, exact=True).first.wait_for()
        return "Agent save/search and browser search return verbatim Unicode and whitespace"

    async def rule_owner_acceptance(self):
        (token,) = self.need("token")
        async with stock_client(self.instance.origin, token) as (session, _initialized):
            await tool(session, "rules_get", {"effective": True})
            await tool(
                session,
                "rules_publish",
                {
                    "agent": "*",
                    "rules": [
                        {
                            "key": "release-verbatim",
                            "text": PROPOSED_RULE,
                            "summary": SUMMARY,
                            "kind": "instruction",
                        }
                    ],
                },
            )
        rules = (await api(self.page, "/api/rules"))["rules"]
        rule = next((row for row in rules if row["text"] == PROPOSED_RULE), None)
        require(
            rule is not None and rule["status"] == "proposed",
            "Agent shared rule must await owner acceptance",
        )
        await self.page.goto(self.instance.origin + "/app/#/rules")
        row = self.page.locator(f'[data-rule-id="{rule["id"]}"]')
        await row.get_by_role("button", name=SUMMARY, exact=False).wait_for()
        require(
            not await row.locator("[data-rule-full]").is_visible(),
            "Rule full text should start collapsed",
        )
        await row.locator('[data-rule-action="expand"]').click()
        await row.get_by_text(PROPOSED_RULE, exact=True).wait_for()
        async with self.page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/action")
        ):
            await row.locator('[data-rule-action="accept"]').click()
        accepted = next(
            r for r in (await api(self.page, "/api/rules"))["rules"] if r["id"] == rule["id"]
        )
        require(accepted["status"] == "accepted", "Rules page owner acceptance was not persisted")
        async with stock_client(self.instance.origin, token) as (session, initialized):
            require(
                PROPOSED_RULE in (initialized.instructions or ""),
                "New session omitted owner-accepted rule",
            )
            require(
                PROPOSED_RULE in await tool(session, "rules_get", {"effective": True}),
                "Agent did not receive accepted rule",
            )
        self.values["rule"] = rule
        return "Summary and expandable full text checked; owner accepts on Rules; agent receives accepted text"

    async def missing_login_token(self):
        async with await self.browser.new_context() as context:
            page = await context.new_page()
            response = await page.goto(self.instance.origin + "/app/login")
            require(response.status == 200, "GET /app/login without t must show the sign-in page")
            await page.get_by_role("heading", name="Sign in", exact=True).wait_for()
            require(
                (await context.request.get(self.instance.origin + "/api/me")).status == 401,
                "Missing t created a session",
            )
            name = "missing_login_token.png"
            await page.screenshot(path=str(self.output / name), full_page=True)
            self.report.results["missing_login_token"].screenshot = name
        return "No-token login opens sign-in and stays anonymous"

    async def logout_login(self):
        (original,) = self.need("signup")
        await self.page.goto(self.instance.origin + "/app/#/settings")
        async with self.page.expect_response(lambda r: r.url.endswith("/app/logout")):
            await self.page.get_by_role("button", name="Sign out", exact=True).first.click()
        require(
            (await self.page.context.request.get(self.instance.origin + "/api/me")).status == 401,
            "Logout left the person authenticated",
        )
        again = await email_login(self.page, self.instance, self.email)
        require(
            (again["user"], again["workspace"]) == (original["user"], original["workspace"]),
            "Login did not return to the same workspace",
        )
        return "Logout invalidates session; new email link restores the same identity/workspace"

    async def second_user_isolation(self):
        original, space, memory, rule = self.need("signup", "space", "memory", "rule")
        async with await self.browser.new_context() as context:
            other = await context.new_page()
            identity = await email_login(other, self.instance, "second-" + self.email, signup=True)
            require(
                identity["workspace"] != original["workspace"],
                "Second user shares the first workspace",
            )
            boards = (await api(other, "/api/boards"))["boards"]
            require(
                space["slug"] not in {b["slug"] for b in boards},
                "Second user sees the first user's space",
            )
            require(
                (await api(other, "/api/memory/search?q=caf%C3%A9%20release%20memory"))["results"]
                == [],
                "Second user sees the first user's memories",
            )
            require(
                rule["id"] not in {r["id"] for r in (await api(other, "/api/rules"))["rules"]},
                "Second user sees the first user's rule",
            )
            for path in (
                f"/api/boards/{space['slug']}/cards",
                f"/api/memory/{quote(memory['id'], safe='')}",
                f"/api/rules/{rule['id']}/history",
            ):
                response = await context.request.get(self.instance.origin + path)
                require(
                    response.status == 404,
                    f"Second user direct access must hide {path.split('/')[2]} object",
                )
            second_token = (
                await api(other, "/api/connections", "POST", {"name": "second-agent"}, 201)
            )["token"]
            async with stock_client(self.instance.origin, second_token) as (session, initialized):
                require(
                    PROPOSED_RULE not in (initialized.instructions or ""),
                    "MCP initialize crossed user boundary",
                )
                require(
                    PROPOSED_RULE not in await tool(session, "rules_get"),
                    "MCP rules crossed user boundary",
                )
                require(
                    "café"
                    not in await tool(session, "memory_search", {"query": "café release memory"}),
                    "MCP search crossed user boundary",
                )
                require(
                    space["slug"] not in await tool(session, "board_list"),
                    "MCP boards crossed user boundary",
                )
                await tool(session, "memory_get", {"id": memory["id"]}, error=True)
            name = "second_user_isolation.png"
            await other.screenshot(path=str(self.output / name), full_page=True)
            self.report.results["second_user_isolation"].screenshot = name
        return "Independent email user and MCP client cannot list, search or directly read first-user data"

    async def wing_scope(self):
        self.need("signup")
        memories = {}
        for theme in ("inbox", "outside"):
            memories[theme] = await api(
                self.page,
                "/api/memory",
                "POST",
                {"theme": theme, "text": f"Wing release canary {theme}"},
                201,
            )
        result = await api(
            self.page,
            "/api/connections",
            "POST",
            {"name": "wing-agent", "read_wings": ["inbox"]},
            201,
        )
        async with stock_client(self.instance.origin, result["token"]) as (session, initialized):
            require(
                "canary outside" not in (initialized.instructions or ""),
                "Startup crossed wing boundary",
            )
            found = await tool(session, "memory_search", {"query": "Wing release canary"})
            require(
                "canary inbox" in found and "canary outside" not in found,
                "Wing search did not include only allowed content",
            )
            require(
                "canary inbox"
                in await tool(session, "memory_get", {"id": memories["inbox"]["id"]}),
                "Allowed direct memory read failed",
            )
            await tool(session, "memory_get", {"id": memories["outside"]["id"]}, error=True)
            await tool(
                session,
                "memory_search",
                {"query": "Wing release canary", "theme": "outside"},
                error=True,
            )
        return "Owner-selected read_wings restricts stock MCP initialize, search and explicit wing access"

    async def synthetic_social_signup(self):
        async with await self.browser.new_context() as context:
            page = await context.new_page()
            await social_login(page, self.instance, "social-" + self.email)
            await page.locator('.col[data-status="todo"]').wait_for()
            name = "synthetic_social_signup.png"
            await page.screenshot(path=str(self.output / name), full_page=True)
            self.report.results["synthetic_social_signup"].screenshot = name
        return "New synthetic GitHub identity signed up through normal start/callback routes; real provider NOT TESTED"

    async def core(self):
        async with await self.browser.new_context(
            viewport={"width": 1280, "height": 900}
        ) as context:
            self.page = await context.new_page()
            self.page.set_default_timeout(10000)
            for name in CHECKS[:-2]:
                await self.check(name, getattr(self, name if name != "email_signup" else "signup"))


async def quota_journey(browser, output, report, name):
    # Separate empty instances prevent a storage denial from masking voice usage.
    limits = {"storage_limit": 0} if name == "storage_limit" else {"voice_limit": 1}
    with throwaway_instance(**limits) as instance:
        journey = Journey(browser, instance, output, report)
        async with await browser.new_context() as context:
            journey.page = await context.new_page()
            journey.page.set_default_timeout(10000)

            async def check():
                await social_login(journey.page, instance, name + "@example.test")
                plan = await api(journey.page, "/api/plan")
                require(
                    plan["enabled"] and plan["plan"] == "free", "New signup lacks the free plan"
                )
                if name == "storage_limit":
                    require(plan["storage"]["limit"] == 0, "Storage fixture budget was not applied")
                    response = await context.request.post(
                        instance.origin + "/api/memory", data={"text": "Quota canary"}
                    )
                    require(response.status == 507, "Exhausted storage must return HTTP 507")
                    payload = await response.json()
                    require(
                        payload.get("limit") == "storage"
                        and "storage is full" in payload.get("error", ""),
                        "Storage limit needs an actionable message",
                    )
                else:
                    require(
                        plan["transcription"]["limit"] == 1, "Voice fixture budget was not applied"
                    )
                    await api(journey.page, "/api/voice", "PATCH", {"enabled": True})
                    endpoint = instance.origin + "/api/voice/transcribe?lang=en"
                    kwargs = {
                        "data": b"\0\0" * 16000,
                        "headers": {"Content-Type": "application/octet-stream"},
                    }
                    require(
                        (await context.request.post(endpoint, **kwargs)).status == 200,
                        "First in-budget voice request failed",
                    )
                    response = await context.request.post(endpoint, **kwargs)
                    require(
                        response.status in (402, 429),
                        "Exhausted voice budget must reject the second one-second clip",
                    )
                    payload = await response.json()
                    require(
                        "voice transcription" in payload.get("error", "")
                        and "UTC" in payload["error"],
                        "Voice cap needs a clear reset message",
                    )
                    require(
                        (await api(journey.page, "/api/plan"))["transcription"]["used_today"] == 1,
                        "Rejected voice clip changed usage",
                    )
                await journey.page.goto(instance.origin + "/app/#/settings")
                await journey.page.get_by_role("heading", name="Free plan", exact=True).wait_for()
                return payload["error"]

            await journey.check(name, check)


async def run(output, report):
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            with throwaway_instance() as instance:
                await Journey(browser, instance, output, report).core()
            for name in CHECKS[-2:]:
                await quota_journey(browser, output, report, name)
        finally:
            await browser.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="Private report/screenshot directory"
    )
    parser.add_argument("--timeout", type=int, default=300, help="Overall deadline in seconds")
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()
    report = Report(CHECKS, revision=revision)
    try:
        asyncio.run(asyncio.wait_for(run(args.output, report), timeout=args.timeout))
    except Exception as exc:
        # Report remains incomplete/non-passing, including missing dependencies.
        report.runner_error = f"Runner did not complete: {type(exc).__name__}"
    finally:
        (args.output / "report.json").write_text(json.dumps(report.payload(), indent=2) + "\n")
        (args.output / "report.md").write_text(report.markdown())
    print(f"{'PASS' if report.passed else 'FAIL'}: {args.output / 'report.md'}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
