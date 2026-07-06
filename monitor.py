#!/usr/bin/env python3
"""Poll a Nitter feed for PS5 restock tweets and trigger alerts."""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


STATUS_ID_RE = re.compile(r"/status/(\d+)")
HTML_TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


@dataclass
class Tweet:
    tweet_id: str
    text: str
    url: str
    published_at: str = ""


@dataclass
class Config:
    nitter_base_url: str
    username: str
    poll_min_seconds: float
    poll_max_seconds: float
    keyword_rules: list[tuple[str, ...]]
    state_file: Path
    http_timeout_seconds: float
    macos_notifications: bool
    alert_on_startup: bool
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_from_number: str
    alert_to_number: str
    twilio_voice_message: str

    @property
    def twilio_enabled(self) -> bool:
        return all(
            [
                self.twilio_account_sid,
                self.twilio_auth_token,
                self.twilio_from_number,
                self.alert_to_number,
            ]
        )

    @property
    def feed_urls(self) -> list[str]:
        base = self.nitter_base_url.rstrip("/")
        user = self.username.lstrip("@")
        return [f"{base}/{user}/rss", f"{base}/{user}"]


def load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default

    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc


def parse_keyword_rules(raw: str) -> list[tuple[str, ...]]:
    rules: list[tuple[str, ...]] = []

    for chunk in raw.split(";"):
        terms = tuple(
            term.strip().lower()
            for term in chunk.split(",")
            if term.strip()
        )
        if terms:
            rules.append(terms)

    if not rules:
        raise ValueError("KEYWORD_RULES must contain at least one rule.")
    return rules


def load_config() -> Config:
    load_dotenv(Path(".env"))

    poll_min_seconds = env_float("POLL_MIN_SECONDS", 2)
    poll_max_seconds = env_float("POLL_MAX_SECONDS", 5)
    if poll_min_seconds <= 0 or poll_max_seconds <= 0:
        raise ValueError("Polling intervals must be greater than zero.")
    if poll_min_seconds > poll_max_seconds:
        raise ValueError("POLL_MIN_SECONDS cannot be greater than POLL_MAX_SECONDS.")

    keyword_rules = parse_keyword_rules(
        os.getenv(
            "KEYWORD_RULES",
            "ps5,amazon;ps5,restock;ps5,flash sale;ps5,slim digital",
        )
    )

    return Config(
        nitter_base_url=os.getenv("NITTER_BASE_URL", "https://nitter.net"),
        username=os.getenv("NITTER_USERNAME", "ICGOriginal"),
        poll_min_seconds=poll_min_seconds,
        poll_max_seconds=poll_max_seconds,
        keyword_rules=keyword_rules,
        state_file=Path(os.getenv("STATE_FILE", ".state/icg_last_seen.json")),
        http_timeout_seconds=env_float("HTTP_TIMEOUT_SECONDS", 10),
        macos_notifications=env_bool("MACOS_NOTIFICATIONS", sys.platform == "darwin"),
        alert_on_startup=env_bool("ALERT_ON_STARTUP", False),
        twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
        twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
        twilio_from_number=os.getenv("TWILIO_FROM_NUMBER", ""),
        alert_to_number=os.getenv("ALERT_TO_NUMBER", ""),
        twilio_voice_message=os.getenv(
            "TWILIO_VOICE_MESSAGE",
            "PS5 restock alert from I C G. Check the latest tweet now.",
        ),
    )


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def request_bytes(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_latest_tweets(config: Config) -> list[Tweet]:
    errors: list[str] = []

    for url in config.feed_urls:
        try:
            payload = request_bytes(url, config.http_timeout_seconds)
            if url.rstrip("/").endswith("/rss"):
                tweets = parse_rss(payload, fallback_base=config.nitter_base_url)
                if tweets:
                    return tweets
            tweets = parse_html(payload.decode("utf-8", errors="ignore"), url)
            if tweets:
                return tweets
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {exc}")

    raise RuntimeError("Unable to fetch tweets from Nitter.\n" + "\n".join(errors))


def parse_rss(payload: bytes, fallback_base: str) -> list[Tweet]:
    root = ET.fromstring(payload)
    tweets: list[Tweet] = []

    for item in root.findall(".//item"):
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or "").strip()
        title = (item.findtext("title") or "").strip()
        description = strip_html(item.findtext("description") or "")
        published_at = (item.findtext("pubDate") or "").strip()
        tweet_id = extract_status_id(link or guid)
        if not tweet_id:
            continue

        final_url = absolutize_url(link or guid, fallback_base)
        text = normalize_spaces("\n".join(part for part in [title, description] if part))
        tweets.append(
            Tweet(
                tweet_id=tweet_id,
                text=text,
                url=final_url,
                published_at=published_at,
            )
        )

    return tweets


def parse_html(document: str, page_url: str) -> list[Tweet]:
    cards = re.findall(
        r'(?s)<div class="timeline-item[^"]*".*?</div>\s*</div>\s*</div>',
        document,
    )
    tweets: list[Tweet] = []

    for card in cards:
        links = STATUS_ID_RE.findall(card)
        if not links:
            continue

        content_match = re.search(
            r'(?s)<div class="tweet-content[^"]*">(.*?)</div>',
            card,
        )
        date_match = re.search(
            r'(?s)<span class="tweet-date".*?title="([^"]+)"',
            card,
        )
        href_match = re.search(r'href="([^"]*/status/\d+[^"]*)"', card)
        if not href_match:
            continue

        tweet_id = links[0]
        text = strip_html(content_match.group(1) if content_match else "")
        text = normalize_spaces(text)
        tweet_url = absolutize_url(html.unescape(href_match.group(1)), page_url)
        tweets.append(
            Tweet(
                tweet_id=tweet_id,
                text=text,
                url=tweet_url,
                published_at=date_match.group(1) if date_match else "",
            )
        )

    return tweets


def strip_html(value: str) -> str:
    value = html.unescape(value)
    value = HTML_TAG_RE.sub(" ", value)
    return normalize_spaces(value)


def normalize_spaces(value: str) -> str:
    return SPACE_RE.sub(" ", value).strip()


def extract_status_id(value: str) -> str:
    match = STATUS_ID_RE.search(value)
    return match.group(1) if match else ""


def absolutize_url(value: str, fallback_base: str) -> str:
    return urllib.parse.urljoin(fallback_base.rstrip("/") + "/", value)


def read_state(state_file: Path) -> dict[str, str]:
    if not state_file.exists():
        return {}

    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def write_state(state_file: Path, state: dict[str, str]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")


def matches_rules(text: str, rules: Iterable[tuple[str, ...]]) -> bool:
    normalized = text.lower()
    return any(all(term in normalized for term in rule) for rule in rules)


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def send_desktop_notification(title: str, body: str) -> None:
    script = f'display notification "{escape_applescript(body)}" with title "{escape_applescript(title)}"'
    try:
        subprocess.run(["osascript", "-e", script], check=False)
    except OSError as exc:
        log(f"Desktop notification failed: {exc}")


def escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def trigger_twilio_call(config: Config, item: Tweet) -> None:
    if not config.twilio_enabled:
        return

    account_sid = config.twilio_account_sid
    message = truncate(config.twilio_voice_message, 180)
    twimlet_url = "https://twimlets.com/message?" + urllib.parse.urlencode(
        {"Message[0]": message}
    )
    endpoint = (
        f"https://api.twilio.com/2010-04-01/Accounts/"
        f"{urllib.parse.quote(account_sid)}/Calls.json"
    )
    payload = urllib.parse.urlencode(
        {
            "To": config.alert_to_number,
            "From": config.twilio_from_number,
            "Url": twimlet_url,
        }
    ).encode("utf-8")

    request = urllib.request.Request(endpoint, data=payload, method="POST")
    credentials = (
        f"{config.twilio_account_sid}:{config.twilio_auth_token}".encode("utf-8")
    )
    auth_header = base64.b64encode(credentials).decode("ascii")
    request.add_header("Authorization", f"Basic {auth_header}")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(
            request,
            timeout=config.http_timeout_seconds,
        ) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="ignore").strip()
        if exc.code == 401:
            raise RuntimeError(
                "Twilio rejected the request with 401 Unauthorized. "
                "Check TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in Railway, "
                "and make sure you are using live account credentials."
                + (f" Response: {details}" if details else "")
            ) from exc
        raise RuntimeError(
            f"Twilio call failed with HTTP {exc.code}."
            + (f" Response: {details}" if details else "")
        ) from exc

    log(f"Twilio call triggered for tweet {item.tweet_id}.")


def alert(config: Config, item: Tweet) -> None:
    title = "PS5 Alert"
    body = truncate(item.text or "Matching tweet detected.", 200)
    log(f"ALERT {item.tweet_id}: {body}")
    log(f"Tweet URL: {item.url}")

    if config.macos_notifications:
        try:
            send_desktop_notification(title, body)
        except Exception as exc:  # noqa: BLE001
            log(f"Desktop notification error: {exc}")
    if config.twilio_enabled:
        try:
            trigger_twilio_call(config, item)
        except Exception as exc:  # noqa: BLE001
            log(f"Twilio alert error: {exc}")


def process_once(config: Config, state: dict[str, str]) -> None:
    tweets = fetch_latest_tweets(config)
    if not tweets:
        log("No tweets found in the fetched response.")
        return

    latest_id = tweets[0].tweet_id
    last_seen_id = state.get("last_seen_id", "")

    if not last_seen_id:
        state["last_seen_id"] = latest_id
        write_state(config.state_file, state)
        log(f"State primed with latest tweet {latest_id}.")
        if config.alert_on_startup and matches_rules(tweets[0].text, config.keyword_rules):
            alert(config, tweets[0])
        return

    if latest_id == last_seen_id:
        log(f"No new tweet. Latest remains {latest_id}.")
        return

    unseen: list[Tweet] = []
    for tweet in tweets:
        if tweet.tweet_id == last_seen_id:
            break
        unseen.append(tweet)

    if not unseen:
        log("Timeline changed, but there were no unseen tweets on the current page.")
        state["last_seen_id"] = latest_id
        write_state(config.state_file, state)
        return

    for tweet in reversed(unseen):
        log(f"New tweet {tweet.tweet_id}: {truncate(tweet.text, 160)}")
        if matches_rules(tweet.text, config.keyword_rules):
            alert(config, tweet)
        else:
            log("Tweet did not match keyword rules.")

    state["last_seen_id"] = latest_id
    write_state(config.state_file, state)


def run_loop(config: Config) -> None:
    state = read_state(config.state_file)
    log(
        "Watching "
        f"@{config.username} via {config.nitter_base_url} every "
        f"{config.poll_min_seconds}-{config.poll_max_seconds} seconds."
    )
    log(
        "Keyword rules: "
        + "; ".join(", ".join(rule) for rule in config.keyword_rules)
    )

    while True:
        try:
            process_once(config, state)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"Polling error: {exc}")

        time.sleep(random.uniform(config.poll_min_seconds, config.poll_max_seconds))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Watch a Nitter feed and alert on matching PS5 tweets."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Fetch once, update state, and exit.",
    )
    args = parser.parse_args()

    try:
        config = load_config()
        state = read_state(config.state_file)
        if args.once:
            process_once(config, state)
            return 0
        run_loop(config)
        return 0
    except KeyboardInterrupt:
        log("Stopped by user.")
        return 130
    except Exception as exc:  # noqa: BLE001
        log(f"Fatal error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
