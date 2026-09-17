"""Application configuration. All secrets and tunables come from the environment / .env file."""

from __future__ import annotations

import re
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip().lstrip("@") for v in str(value).split(",") if v.strip()]


def _csv_int(value: str | None) -> list[int]:
    out: list[int] = []
    for v in _csv(value):
        try:
            out.append(int(v))
        except ValueError:
            continue
    return out


PROJECT_ROOT = Path(__file__).resolve().parent.parent
# The .env is resolved relative to the project, not the shell's working directory, so
# `python /full/path/app/main.py` from anywhere (IDE run buttons, launchd) still loads it.
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ENV_FILE), env_file_encoding="utf-8", extra="ignore")

    # Core
    app_env: str = "development"
    log_level: str = "INFO"
    timezone: str = "Asia/Kolkata"
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-5.6-terra"
    openai_timeout_seconds: int = 120
    openai_max_retries: int = 4  # SDK backoff on 429 / 5xx before we call the service unavailable
    ai_max_concurrency: int = 6  # AI reads in flight at once; the rest queue instead of tripping rate limits
    ai_classify_unknown_betix_replies: bool = True
    video_keyframes_enabled: bool = True
    video_keyframe_count: int = 4

    # DB / queue
    database_url: str = "sqlite+aiosqlite:///./data/verifier.db"
    redis_url: str = "redis://localhost:6379/0"
    # true = run jobs in-process (no Redis; dev/single-process only). false = arq worker on Redis.
    inline_jobs: bool = False

    # ------------------------------------------------------------------
    # TELEGRAM — two SEPARATE credential sets:
    #
    #  (1) Bot API credentials (always required). One bot token from @BotFather.
    #      Used for: the input bot, admin notifications, and — in *_MODE=bot —
    #      posting to and monitoring the Betix group.
    #
    #  (2) Telegram user/client API credentials (OPTIONAL, MTProto via Telethon).
    #      TELEGRAM_API_ID + TELEGRAM_API_HASH from https://my.telegram.org and a
    #      TELEGRAM_SESSION string from scripts/create_telegram_session.py.
    #      Used ONLY when BETIX_POST_MODE=user and/or BETIX_MONITOR_MODE=user,
    #      i.e. when the Bot API cannot read/post in the Betix group. Keep them
    #      configured even in bot mode so the switch is a one-line change.
    # ------------------------------------------------------------------
    # (1) Bot API
    telegram_bot_token: str = ""
    admin_telegram_user_ids: str = ""
    # true = customers may message the bot directly (a case is created for the sender). false = only the
    # admins above may submit; they forward the customer's messages, and the case belongs to the ORIGINAL sender.
    allow_customer_direct_submissions: bool = False
    admin_notify_chat_id: str = ""
    case_collection_window_minutes: int = 30
    case_debounce_seconds: int = 60
    # Payment screenshot + mobile + bank statement + payment video must ALL be present before processing.
    require_all_evidence: bool = True
    # CASE INPUT + COLLECTION: the FIRST message in a chat is HELD for this many seconds (never restarted by later
    # messages). Then everything held becomes ONE case, ONE card is sent and the case is checked once: all four
    # required items present -> processed; anything missing -> shown, and the case waits for it.
    # FORCE SEND: once the payment screenshot AND the mobile are in, a missing bank statement / payment video no longer
    # holds the case: after this many seconds with no new input (every new file restarts the wait) the case is
    # processed and sent to Betix with what it has. Late files still go out as replies. 0 = wait for everything.
    # A payment screenshot must actually SHOW a UTR / transaction id. No UTR readable -> the operator is asked for a
    # clear screenshot and nothing is searched or sent. A UTR is never assumed from anywhere else.
    require_screenshot_utr: bool = True
    ai_retry_seconds: int = 600  # a case held because the AI service was unavailable is retried this often
    # Database pool. Every concurrent case, job and command holds one connection while it works; the defaults
    # (5 + 10) stall the whole bot once a handful of cases run at the same time.
    db_pool_size: int = 20
    db_max_overflow: int = 20
    db_pool_timeout_seconds: int = 30
    # Telegram send spacing (app/telegram/throttle.py): 20/min into a group, ~1/s into a private chat, ~30/s overall
    tg_group_send_interval_seconds: float = 3.0
    tg_private_send_interval_seconds: float = 0.4
    tg_sends_per_second: int = 25
    add_session_seconds: int = 600  # an /add stays open this long at most; it closes itself once the files are in
    inline_job_workers: int = 8  # jobs running at once when INLINE_JOBS=true (app/workers/runner.py)
    force_send_seconds: int = 30
    collection_seconds: int = 5
    # A forwarded batch is ONE case even when the forwards carry different original senders (the customer's
    # messages relayed by different people): a forward arriving within this many seconds of the last input of the
    # collecting case joins it. Screenshot/mobile conflicts still split cases.
    batch_join_seconds: int = 30
    registration_regex: str = (
        r"^(?:reg(?:istration)?\.?(?:\s*(?:no\.?|number|#))?[:#\s]+)?([A-Za-z0-9][A-Za-z0-9\-_/]{3,39})$"
    )

    # Betix group
    betix_group_chat_id: str = ""
    betix_group_username: str = ""
    betix_post_mode: Literal["bot", "user"] = "bot"
    betix_monitor_mode: Literal["bot", "user"] = "bot"
    # OPTIONAL restriction. Empty (recommended) = any member of the configured Betix group
    # may confirm a payment. Non-empty = only these members may.
    authorized_betix_reviewer_ids: str = ""
    authorized_betix_reviewer_usernames: str = ""
    # Verify group membership of a human confirmer via the Bot API (getChatMember). If the lookup is
    # impossible (user mode / API error) the fact that the message was posted in the group is used.
    betix_verify_membership: bool = True
    betix_membership_cache_minutes: int = 10
    # Our OWN Telegram accounts (comma separated ids / usernames): never treated as a Betix confirmer.
    # The input bot's own id is added automatically at startup.
    our_telegram_ids: str = ""
    our_telegram_usernames: str = ""
    betix_system_bot_usernames: str = "betixpay_cs_bot"
    betix_system_bot_ids: str = ""
    # either  = the Betix system bot OR any human member of the Betix group confirms  (default)
    # strict  = BOTH the system bot AND a human group member must confirm
    # monitor = alias of either (kept for compatibility)
    confirmation_mode: Literal["either", "strict", "monitor"] = "either"
    betix_screenshot_caption_template: str = "{betex_order_id}"
    betix_pi_check: bool = True
    betix_pi_command_template: str = "/pi {order_id}"
    pi_check_max_orders: int = 4
    pi_check_timeout_seconds: int = 120
    # After a UPI match settles the order, delete from the Betix group: "matched" = that order's /pi message and the
    # Betix bot's direct reply to it | "all" = every /pi query of the case and their bot replies | "off".
    pi_cleanup: Literal["matched", "all", "off"] = "matched"
    upi_ending_min_chars: int = 3
    betix_followup_text: str = "Any update?"  # both follow-ups, sent as replies to the screenshot post
    # Bank statement / payment video in the Betix group. They are only ever sent as REPLIES to the screenshot post:
    #   always     = statement (+ its password) and video go right after the screenshot, as replies  [default]
    #   on_request = kept internal; sent only once Betix asks for more evidence
    #   never      = the group gets ONLY the screenshot + ILLUN id
    betix_extra_evidence_policy: Literal["always", "on_request", "never"] = "always"
    betex_order_id_regex: str = r"\bILLUN-\d{8,20}\b"
    betix_plat_order_regex: str = r"\bPI[0-9a-z]{10,24}\b"
    betix_proximity_window_minutes: int = 10

    # (2) Telegram user/client API (Telethon) — only consulted in user mode
    telegram_api_id: int | None = None
    telegram_api_hash: str = ""
    telegram_session: str = ""

    # Follow-ups
    # Follow-ups ("Any update?" replies to our screenshot post) at these minutes AFTER POSTING, while the payment
    # is not solved: 2 h, 8 h, 24 h, 48 h. Confirmation cancels whatever is left immediately.
    followup_schedule_minutes: str = "120,480,1440,2880"
    escalation_delay_minutes: int = 480  # manual-review alert this long after the LAST follow-up
    # Seconds-resolution overrides. Empty in production; set them to rehearse the reminder chain in under a
    # minute ("10,20,30") without waiting half an hour for the first reply.
    followup_schedule_seconds: str = ""
    escalation_delay_seconds: float = 0.0
    followup_sweep_seconds: int = 30  # how often due follow-ups are looked for
    followup_max_attempts: int = 3  # a send that fails is retried on the next sweeps, not given up on
    followup_retry_seconds: int = 60  # how long to wait before retrying a failed send

    # Illunise admin
    illunise_admin_base_url: str = "https://illunise.in"
    illunise_admin_username: str = ""
    illunise_admin_password: str = ""
    admin_auth_mode: Literal["auto", "manual"] = "auto"
    admin_headless: bool = True
    admin_storage_state_path: str = "data/browser/illunise_state.json"
    admin_selectors_file: str = "config/admin_selectors.yaml"
    admin_nav_timeout_ms: int = 30000
    admin_browser_pages: int = 3  # logged-in Illunise tabs searching in parallel; more searches wait their turn
    admin_debug_artifacts_dir: str = "data/browser/debug"

    # Matching (deterministic, see app/admin/matcher.py)
    order_match_threshold: float = 0.90
    order_match_ambiguity_gap: float = 0.10
    # The order is created BEFORE the customer pays: accept orders created between MIN and MAX minutes
    # before the payment time, with a tolerance for clock differences between the screenshot and the server.
    order_time_min_before_payment: int = 0
    order_time_max_before_payment: int = 30
    payment_time_tolerance_minutes: int = 3
    # Two orders both created before the payment: the closer one wins when it is at least this much closer.
    order_time_tiebreak_minutes: int = 2
    # Kept for compatibility with older configs; the before-payment window above is what is used.
    payment_time_window_minutes: int = 15
    # The customer pays a "padded" amount (₹13,999.35 for a ₹14,000 order); tolerance in currency units.
    order_amount_tolerance: float = 1.0
    betix_gateway_name: str = "BetixPay"
    # Order statuses that are compatible with a payment being verified (comma separated, case-insensitive)
    compatible_order_statuses: str = "pending,success,processing,paid,initiated,created"
    # Orders in these statuses are NOT ruled out: the customer often pays after the order's window closes and the
    # panel then shows "Expired" while still holding the payment's UTR. Such an order matches (fully when the UTR
    # is identical, half otherwise) and goes to Betix for manual confirmation.
    expired_order_statuses: str = "expired,timeout,timed out,time out"
    # An order whose Illunise status is one of these was already confirmed: do NOT post it to Betix, just say so.
    already_success_statuses: str = "success,successful,paid,completed"
    match_weight_registration: float = 0.30
    match_weight_amount: float = 0.25
    match_weight_time: float = 0.20
    match_weight_gateway: float = 0.10
    match_weight_utr: float = 0.10
    match_weight_status: float = 0.05
    match_weight_upi: float = 0.0
    match_weight_payer: float = 0.0

    # Evidence
    evidence_dir: str = "data/evidence"
    evidence_retention_days: int = 90
    evidence_max_download_mb: int = 20

    @field_validator("admin_notify_chat_id", "betix_group_chat_id", mode="before")
    @classmethod
    def _strip(cls, v):  # noqa: D102
        return str(v).strip() if v is not None else ""

    @field_validator("telegram_api_id", mode="before")
    @classmethod
    def _blank_int_is_none(cls, v):  # noqa: D102
        if v is None or str(v).strip() == "":
            return None
        return int(v)

    # ----- derived helpers -----
    @property
    def admin_user_ids(self) -> set[int]:
        return set(_csv_int(self.admin_telegram_user_ids))

    @property
    def reviewer_ids(self) -> set[int]:
        return set(_csv_int(self.authorized_betix_reviewer_ids))

    @property
    def reviewer_usernames(self) -> set[str]:
        return {u.lower() for u in _csv(self.authorized_betix_reviewer_usernames)}

    @property
    def our_ids(self) -> set[int]:
        return set(_csv_int(self.our_telegram_ids))

    @property
    def our_usernames(self) -> set[str]:
        return {u.lower() for u in _csv(self.our_telegram_usernames)}

    @property
    def system_bot_usernames(self) -> set[str]:
        return {u.lower() for u in _csv(self.betix_system_bot_usernames)}

    @property
    def system_bot_ids(self) -> set[int]:
        return set(_csv_int(self.betix_system_bot_ids))

    @property
    def betix_chats(self) -> list[int | str]:
        """Every Betix group the bot works with: BETIX_GROUP_CHAT_ID, comma separated (+ BETIX_GROUP_USERNAME).
        The first one is the default a new case is posted to; a case remembers its own group afterwards."""
        out: list[int | str] = []
        for x in self.betix_group_chat_id.split(","):
            x = x.strip()
            if not x:
                continue
            try:
                out.append(int(x))
            except ValueError:
                out.append(x)
        if self.betix_group_username:
            out.append("@" + self.betix_group_username.lstrip("@"))
        return out

    @property
    def betix_chat_ids(self) -> set[int]:
        return {c for c in self.betix_chats if isinstance(c, int)}

    @property
    def betix_chat(self) -> int | str | None:
        """The default group (the first configured one)."""
        chats = self.betix_chats
        return chats[0] if chats else None

    @property
    def notify_chats(self) -> list[int | str]:
        """Every chat that gets the alerts and confirmations: ADMIN_NOTIFY_CHAT_ID, comma separated."""
        out: list[int | str] = []
        for x in self.admin_notify_chat_id.split(","):
            x = x.strip()
            if not x:
                continue
            try:
                out.append(int(x))
            except ValueError:
                out.append(x)
        return out

    @property
    def notify_chat(self) -> int | str | None:
        """The first notify chat (kept for callers that want a single id)."""
        chats = self.notify_chats
        return chats[0] if chats else None

    @property
    def betex_order_id_pattern(self) -> re.Pattern[str]:
        return re.compile(self.betex_order_id_regex, re.I)

    @property
    def plat_order_pattern(self) -> re.Pattern[str]:
        return re.compile(self.betix_plat_order_regex)

    @property
    def registration_pattern(self) -> re.Pattern[str]:
        return re.compile(self.registration_regex, re.I)

    @property
    def match_weights(self) -> dict[str, float]:
        return {
            "registration": self.match_weight_registration,
            "amount": self.match_weight_amount,
            "time": self.match_weight_time,
            "gateway": self.match_weight_gateway,
            "utr": self.match_weight_utr,
            "status": self.match_weight_status,
            "upi": self.match_weight_upi,
            "payer": self.match_weight_payer,
        }

    @property
    def compatible_statuses(self) -> set[str]:
        return {s.strip().lower() for s in self.compatible_order_statuses.split(",") if s.strip()}

    @property
    def followup_offsets(self) -> list[int]:
        out = []
        for x in self.followup_schedule_minutes.split(","):
            x = x.strip()
            if x.isdigit() and int(x) > 0:
                out.append(int(x))
        return sorted(set(out))

    @property
    def followup_delays(self) -> list[timedelta]:
        """How long after the post each follow-up is due. FOLLOWUP_SCHEDULE_SECONDS wins when it is set."""
        secs = []
        for x in self.followup_schedule_seconds.split(","):
            x = x.strip()
            try:
                v = float(x)
            except ValueError:
                continue
            if v > 0:
                secs.append(v)
        if secs:
            return [timedelta(seconds=v) for v in sorted(set(secs))]
        return [timedelta(minutes=m) for m in self.followup_offsets]

    @property
    def escalation_delay(self) -> timedelta:
        """How long after the LAST follow-up the manual-review alert is raised."""
        if self.escalation_delay_seconds > 0:
            return timedelta(seconds=self.escalation_delay_seconds)
        return timedelta(minutes=self.escalation_delay_minutes)

    @property
    def expired_statuses(self) -> set[str]:
        return {s.strip().lower() for s in self.expired_order_statuses.split(",") if s.strip()}

    @property
    def success_statuses(self) -> set[str]:
        return {s.strip().lower() for s in self.already_success_statuses.split(",") if s.strip()}

    @property
    def time_rule(self) -> dict[str, int]:
        return {
            "min_before": self.order_time_min_before_payment,
            "max_before": self.order_time_max_before_payment,
            "tolerance": self.payment_time_tolerance_minutes,
        }

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
