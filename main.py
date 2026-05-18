from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import AtAll
from astrbot.api.star import Context, Star
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.star_tools import StarTools


STATE_FILE_NAME = "state.json"
DEFAULT_CHANGELOG_CANDIDATES = [
    "CHANGELOG.md",
    "CHANGELOG",
    "changelog.md",
    "docs/CHANGELOG.md",
    "RELEASE_NOTES.md",
]
DEFAULT_POLL_INTERVAL = 300
DEFAULT_TIMEOUT = 20
DEFAULT_MAX_COMMITS = 10


@dataclass
class TargetConfig:
    umo: str
    enabled: bool = True
    mention_all: bool = False
    prefix: str = ""


@dataclass
class RepoConfig:
    name: str
    enabled: bool = True
    branch: str = ""
    watch_commits: bool = True
    watch_releases: bool = True
    include_commit_diff_url: bool = True
    changelog_enabled: bool = True
    changelog_paths: list[str] = field(default_factory=list)
    target_umos: list[str] = field(default_factory=list)
    silent_on_empty_target: bool = True


@dataclass
class CommitNotification:
    repo_full_name: str
    branch: str
    default_branch: str
    commits: list[dict[str, Any]]
    compare_url: str = ""
    changelog_text: str = ""


@dataclass
class ReleaseNotification:
    repo_full_name: str
    release_name: str
    tag_name: str
    published_at: str
    html_url: str
    body: str
    prerelease: bool
    draft: bool
    changelog_text: str = ""


class GitHubApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: int,
        debug_logger: Callable[..., None] | None = None,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "astrbot-plugin-github-repo-watch",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout_seconds,
            follow_redirects=True,
        )
        self._debug_logger = debug_logger

    async def close(self) -> None:
        await self._client.aclose()

    async def get_repo(self, repo_full_name: str) -> dict[str, Any]:
        return await self._get_json(f"/repos/{repo_full_name}")

    async def list_commits(
        self,
        repo_full_name: str,
        *,
        branch: str,
        per_page: int,
    ) -> list[dict[str, Any]]:
        params = {"per_page": per_page}
        if branch:
            params["sha"] = branch
        return await self._get_json(f"/repos/{repo_full_name}/commits", params=params)

    async def list_releases(
        self,
        repo_full_name: str,
        *,
        per_page: int = 5,
    ) -> list[dict[str, Any]]:
        return await self._get_json(
            f"/repos/{repo_full_name}/releases",
            params={"per_page": per_page},
        )

    async def get_text_file_if_exists(
        self,
        repo_full_name: str,
        path: str,
        *,
        branch: str,
    ) -> str | None:
        params: dict[str, str] = {}
        if branch:
            params["ref"] = branch
        response = await self._request(
            "GET",
            f"/repos/{repo_full_name}/contents/{path}",
            params=params,
        )
        if response.status_code == 404:
            self._debug(
                "github content missing repo=%s path=%s branch=%s",
                repo_full_name,
                path,
                branch,
            )
            return None
        response.raise_for_status()
        payload = response.json()
        if payload.get("type") != "file":
            self._debug(
                "github content is not file repo=%s path=%s payload_type=%s",
                repo_full_name,
                path,
                payload.get("type"),
            )
            return None
        download_url = payload.get("download_url")
        if not download_url:
            self._debug(
                "github content has no download_url repo=%s path=%s",
                repo_full_name,
                path,
            )
            return None
        text_response = await self._request("GET", download_url)
        text_response.raise_for_status()
        return text_response.text

    async def _get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        response = await self._request("GET", path, params=params)
        response.raise_for_status()
        return response.json()

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self._debug(
            "github request method=%s url=%s params=%s",
            method,
            url,
            kwargs.get("params"),
        )
        response = await self._client.request(method, url, **kwargs)
        self._debug(
            "github response status=%s url=%s remaining=%s reset=%s",
            response.status_code,
            str(response.request.url),
            response.headers.get("X-RateLimit-Remaining"),
            response.headers.get("X-RateLimit-Reset"),
        )
        if response.status_code >= 400:
            self._debug(
                "github error body status=%s url=%s body=%s",
                response.status_code,
                str(response.request.url),
                self._truncate_debug_text(response.text),
            )
        return response

    def _debug(self, message: str, *args: Any) -> None:
        if self._debug_logger:
            self._debug_logger(message, *args)

    def _truncate_debug_text(self, text: str, max_length: int = 1200) -> str:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(normalized) <= max_length:
            return normalized
        return normalized[: max(0, max_length - 3)] + "..."


class GitHubRepoWatchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config or AstrBotConfig()
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._state: dict[str, Any] = {"repos": {}, "subscriptions": {}}
        self._client: GitHubApiClient | None = None
        self._client_signature: tuple[str, str, int] | None = None
        self._data_dir = StarTools.get_data_dir("astrbot_plugin_github_repo_watch")
        self._state_file = self._data_dir / STATE_FILE_NAME

    async def initialize(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        await self._load_state()
        await self._recreate_client()
        if self._bool("enabled", True):
            self._start_background_task()
        self._debug(
            "plugin initialized data_dir=%s state_file=%s enabled=%s poll_interval=%s repos=%s targets=%s",
            self._data_dir,
            self._state_file,
            self._bool("enabled", True),
            self._poll_interval_seconds(),
            len(self._load_repo_configs()),
            len(self._load_targets()),
        )

    async def terminate(self) -> None:
        self._debug("plugin terminate requested")
        await self._stop_background_task()
        if self._client:
            await self._client.close()
            self._client = None
            self._debug("http client closed")

    @filter.command("ghwatchumo")
    async def ghwatchumo(self, event: AstrMessageEvent):
        """查看当前会话 UMO"""
        yield event.plain_result(
            "当前会话 UMO:\n"
            f"{event.unified_msg_origin}\n\n"
            "可直接复制到插件配置的推送会话 UMO 中。"
        )

    @filter.command("ghwatchstatus")
    async def ghwatchstatus(self, event: AstrMessageEvent):
        """查看 GitHub 监控插件状态"""
        repos = self._load_repo_configs()
        targets = self._load_targets()
        task_running = self._task is not None and not self._task.done()
        lines = [
            f"启用状态: {'开启' if self._bool('enabled', True) else '关闭'}",
            f"后台任务: {'运行中' if task_running else '未运行'}",
            f"调试模式: {'开启' if self._debug_enabled() else '关闭'}",
            f"轮询间隔: {self._poll_interval_seconds()} 秒",
            f"仓库数量: {len(repos)}",
            f"推送会话数量: {len(targets)}",
        ]
        if repos:
            lines.append("")
            lines.append("仓库列表:")
            for repo in repos:
                lines.append(
                    f"- {repo.name} | commits={'on' if repo.watch_commits else 'off'} | "
                    f"releases={'on' if repo.watch_releases else 'off'} | "
                    f"changelog={'on' if repo.changelog_enabled else 'off'}"
                )
        yield event.plain_result("\n".join(lines))

    @filter.command("ghwatchcheck")
    async def ghwatchcheck(self, event: AstrMessageEvent):
        """立即执行一次 GitHub 仓库检查"""
        await self._ensure_client_fresh()
        result = await self._run_check_cycle(manual=True)
        if result["errors"]:
            yield event.plain_result(
                "本次检查已完成，但存在异常：\n" + "\n".join(result["errors"])
            )
            return
        yield event.plain_result("检查已执行，详情请查看后台日志。")

    @filter.command("ghwatchtest")
    async def ghwatchtest(self, event: AstrMessageEvent):
        """向当前会话发送测试通知"""
        target = self._target_from_umo(event.unified_msg_origin)
        ok = await self._send_chain_to_target(
            target,
            self._build_text_chain(
                target,
                "GitHub Repo Watch 测试通知\n这条消息说明插件可以正常主动推送到当前会话。",
            ),
        )
        if ok:
            yield event.plain_result("测试通知已发送到当前会话。")
        else:
            yield event.plain_result("测试通知发送失败，请查看后台日志。")

    @filter.command("ghwatchsub")
    async def ghwatchsub(self, event: AstrMessageEvent):
        """在当前会话快捷订阅一个仓库"""
        repo_name = self._extract_command_args(event.message_str, ["ghwatchsub"])
        repo_name = self._normalize_repo_name(repo_name)
        if not repo_name:
            yield event.plain_result("用法: /ghwatchsub owner/repo")
            return

        changed = self._ensure_target_exists(event.unified_msg_origin)
        repo_changed = self._ensure_repo_subscription(repo_name, event.unified_msg_origin)
        if changed or repo_changed:
            self.config.save_config()
            await self._ensure_started()

        yield event.plain_result(
            f"已将当前会话订阅到仓库 {repo_name}。\n"
            "后续该仓库的 commit/release 更新会推送到这里。"
        )

    @filter.command("ghwatchunsub")
    async def ghwatchunsub(self, event: AstrMessageEvent):
        """取消当前会话对某个仓库的订阅"""
        repo_name = self._extract_command_args(event.message_str, ["ghwatchunsub"])
        repo_name = self._normalize_repo_name(repo_name)
        if not repo_name:
            yield event.plain_result("用法: /ghwatchunsub owner/repo")
            return

        removed = self._remove_repo_subscription(repo_name, event.unified_msg_origin)
        if removed:
            self.config.save_config()
            yield event.plain_result(f"已取消当前会话对仓库 {repo_name} 的订阅。")
            return
        yield event.plain_result(f"当前会话未订阅仓库 {repo_name}。")

    @filter.command("ghwatchsubs")
    async def ghwatchsubs(self, event: AstrMessageEvent):
        """查看当前会话订阅的仓库"""
        repos = self._repos_for_umo(event.unified_msg_origin)
        if not repos:
            yield event.plain_result("当前会话还没有订阅任何仓库。")
            return
        yield event.plain_result(
            "当前会话已订阅的仓库:\n" + "\n".join(f"- {repo}" for repo in repos)
        )

    def _start_background_task(self) -> None:
        if self._task and not self._task.done():
            self._debug("background task already running")
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._poll_loop(), name="github-repo-watch")
        self._debug("background task started")

    async def _stop_background_task(self) -> None:
        self._stop_event.set()
        if self._task:
            self._debug("stopping background task")
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("github repo watch background task stopped with error")
        self._task = None
        self._debug("background task cleared")

    async def _ensure_started(self) -> None:
        await self._ensure_client_fresh()
        if self._bool("enabled", True) and (self._task is None or self._task.done()):
            self._start_background_task()

    async def _recreate_client(self) -> None:
        if self._client:
            await self._client.close()
        base_url = self._text("github_api_base_url", "https://api.github.com")
        token = self._text("github_token", "")
        timeout_seconds = self._int("request_timeout_seconds", DEFAULT_TIMEOUT)
        self._client = GitHubApiClient(
            base_url=base_url,
            token=token,
            timeout_seconds=timeout_seconds,
            debug_logger=self._debug,
        )
        self._client_signature = (base_url, token, timeout_seconds)
        self._debug(
            "http client recreated base_url=%s timeout=%s token_configured=%s",
            base_url,
            timeout_seconds,
            bool(token),
        )

    async def _ensure_client_fresh(self) -> None:
        signature = (
            self._text("github_api_base_url", "https://api.github.com"),
            self._text("github_token", ""),
            self._int("request_timeout_seconds", DEFAULT_TIMEOUT),
        )
        if self._client is None or self._client_signature != signature:
            self._debug("client signature changed or missing, recreating client")
            await self._recreate_client()

    async def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._debug("poll loop cycle start")
                await self._run_check_cycle(manual=False)
                self._debug("poll loop cycle finished")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("github repo watch poll loop failed")
            try:
                self._debug("poll loop sleeping seconds=%s", self._poll_interval_seconds())
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval_seconds(),
                )
            except asyncio.TimeoutError:
                continue

    async def _run_check_cycle(self, *, manual: bool) -> dict[str, Any]:
        async with self._run_lock:
            repos = self._load_repo_configs()
            if not repos:
                return {"errors": ["未配置任何仓库。"], "results": []}
            targets = self._load_targets()
            if not targets:
                logger.warning("github repo watch has no enabled targets configured")
            self._debug(
                "run check cycle manual=%s repo_count=%s target_count=%s",
                manual,
                len(repos),
                len(targets),
            )

            await self._ensure_client_fresh()
            assert self._client is not None

            results: list[str] = []
            errors: list[str] = []
            commit_notice_count = 0
            release_notice_count = 0

            for repo in repos:
                try:
                    repo_result = await self._check_repo(repo, manual=manual)
                    results.append(repo_result["summary"])
                    commit_notice_count += repo_result["commit_notice_count"]
                    release_notice_count += repo_result["release_notice_count"]
                    self._debug(
                        "repo cycle summary repo=%s summary=%s",
                        repo.name,
                        repo_result["summary"],
                    )
                except httpx.HTTPStatusError as exc:
                    self._log_http_status_error(repo.name, exc)
                    msg = self._format_http_error(exc)
                    logger.warning("[github_repo_watch] repo=%s %s", repo.name, msg)
                    errors.append(f"{repo.name}: {msg}")
                except Exception as exc:
                    logger.exception("failed to check repository %s", repo.name)
                    errors.append(f"{repo.name}: 检查失败 - {exc}")

            self._debug(
                "run check cycle done manual=%s commit_notices=%s release_notices=%s results=%s errors=%s",
                manual,
                commit_notice_count,
                release_notice_count,
                results,
                errors,
            )
            return {
                "errors": errors,
                "results": results,
                "commit_notice_count": commit_notice_count,
                "release_notice_count": release_notice_count,
            }

    async def _check_repo(self, repo: RepoConfig, *, manual: bool) -> dict[str, Any]:
        assert self._client is not None
        repo_meta = await self._client.get_repo(repo.name)
        default_branch = repo_meta.get("default_branch") or ""
        branch = repo.branch or default_branch

        repo_state = self._get_repo_state(repo.name)

        summary_bits: list[str] = [repo.name]
        commit_notice_count = 0
        release_notice_count = 0
        self._debug(
            "checking repo=%s branch=%s default_branch=%s state_keys=%s manual=%s",
            repo.name,
            branch,
            default_branch,
            sorted(repo_state.keys()),
            manual,
        )

        if repo.watch_commits:
            commit_notice = await self._build_commit_notification(
                repo,
                repo_state,
                branch=branch,
                default_branch=default_branch,
                manual=manual,
            )
            if commit_notice:
                await self._send_commit_notification(repo, commit_notice)
                commit_notice_count += 1
                summary_bits.append(f"commits +{len(commit_notice.commits)}")
            else:
                summary_bits.append("commits unchanged")

        if repo.watch_releases:
            release_notice = await self._build_release_notification(
                repo,
                repo_state,
                branch=branch,
                manual=manual,
            )
            if release_notice:
                await self._send_release_notification(repo, release_notice)
                release_notice_count += 1
                summary_bits.append("release updated")
            else:
                summary_bits.append("release unchanged")

        if repo.changelog_enabled:
            await self._refresh_changelog_state(repo, repo_state, branch=branch)

        await self._save_state()

        return {
            "summary": " | ".join(summary_bits),
            "commit_notice_count": commit_notice_count,
            "release_notice_count": release_notice_count,
        }

    async def _build_commit_notification(
        self,
        repo: RepoConfig,
        repo_state: dict[str, Any],
        *,
        branch: str,
        default_branch: str,
        manual: bool,
    ) -> CommitNotification | None:
        assert self._client is not None
        commits = await self._client.list_commits(
            repo.name,
            branch=branch,
            per_page=max(1, self._int("max_commits_per_check", DEFAULT_MAX_COMMITS)),
        )
        if not commits:
            return None

        latest_sha = commits[0]["sha"]
        last_seen_sha = repo_state.get("last_commit_sha", "")
        commit_first_seen = "last_commit_sha" not in repo_state
        repo_state["last_commit_sha"] = latest_sha
        repo_state["last_seen_branch"] = branch
        self._debug(
            "commit state repo=%s branch=%s latest_sha=%s last_seen_sha=%s first_seen=%s",
            repo.name,
            branch,
            latest_sha,
            last_seen_sha,
            commit_first_seen,
        )

        if commit_first_seen and self._bool("skip_initial_history", True) and not manual:
            return None

        new_commits: list[dict[str, Any]] = []
        for item in commits:
            if item["sha"] == last_seen_sha:
                break
            new_commits.append(item)
        new_commits.reverse()
        self._debug(
            "commit diff repo=%s branch=%s fetched=%s new=%s",
            repo.name,
            branch,
            len(commits),
            len(new_commits),
        )

        if not new_commits:
            return None

        compare_url = ""
        if repo.include_commit_diff_url and last_seen_sha:
            compare_url = f"https://github.com/{repo.name}/compare/{last_seen_sha}...{latest_sha}"

        changelog_text = ""
        if repo.changelog_enabled and self._bool("send_changelog_for_commits", True):
            changelog_text = await self._maybe_extract_changelog_delta(repo, repo_state, branch=branch)

        return CommitNotification(
            repo_full_name=repo.name,
            branch=branch,
            default_branch=default_branch,
            commits=new_commits,
            compare_url=compare_url,
            changelog_text=changelog_text,
        )

    async def _build_release_notification(
        self,
        repo: RepoConfig,
        repo_state: dict[str, Any],
        *,
        branch: str,
        manual: bool,
    ) -> ReleaseNotification | None:
        assert self._client is not None
        releases = await self._client.list_releases(repo.name, per_page=5)
        if not releases:
            return None

        latest = next((item for item in releases if not item.get("draft")), None)
        if not latest:
            return None

        release_id = str(latest.get("id", ""))
        last_release_id = str(repo_state.get("last_release_id", ""))
        release_first_seen = "last_release_id" not in repo_state
        repo_state["last_release_id"] = release_id
        self._debug(
            "release state repo=%s latest_release_id=%s last_release_id=%s first_seen=%s",
            repo.name,
            release_id,
            last_release_id,
            release_first_seen,
        )

        if release_first_seen and self._bool("skip_initial_history", True) and not manual:
            return None
        if release_id == last_release_id:
            return None

        changelog_text = ""
        if repo.changelog_enabled and self._bool("send_changelog_for_releases", True):
            changelog_text = await self._maybe_extract_changelog_delta(repo, repo_state, branch=branch)

        body = self._truncate_text(
            latest.get("body") or "",
            self._int("max_release_body_length", 1200),
        )

        return ReleaseNotification(
            repo_full_name=repo.name,
            release_name=latest.get("name") or latest.get("tag_name") or "Unnamed Release",
            tag_name=latest.get("tag_name") or "",
            published_at=latest.get("published_at") or latest.get("created_at") or "",
            html_url=latest.get("html_url") or f"https://github.com/{repo.name}/releases",
            body=body,
            prerelease=bool(latest.get("prerelease")),
            draft=bool(latest.get("draft")),
            changelog_text=changelog_text,
        )

    async def _maybe_extract_changelog_delta(
        self,
        repo: RepoConfig,
        repo_state: dict[str, Any],
        *,
        branch: str,
    ) -> str:
        text, path = await self._fetch_changelog(repo, branch=branch)
        if not text:
            self._debug("no changelog text repo=%s branch=%s", repo.name, branch)
            return ""

        changelog_state = repo_state.setdefault("changelog", {})
        previous_text = str(changelog_state.get("content", ""))
        self._debug(
            "changelog compare repo=%s path=%s previous_len=%s current_len=%s",
            repo.name,
            path,
            len(previous_text),
            len(text),
        )
        if not previous_text:
            return ""

        delta = self._extract_added_prefix(previous_text, text)
        if not delta:
            self._debug("changelog unchanged repo=%s path=%s", repo.name, path)
            return ""
        self._debug(
            "changelog delta repo=%s path=%s delta_len=%s",
            repo.name,
            path,
            len(delta),
        )
        return self._truncate_text(
            delta.strip(),
            self._int("max_changelog_length", 1500),
        )

    async def _refresh_changelog_state(
        self,
        repo: RepoConfig,
        repo_state: dict[str, Any],
        *,
        branch: str,
    ) -> None:
        text, path = await self._fetch_changelog(repo, branch=branch)
        if text is None:
            self._debug("skip changelog state refresh repo=%s branch=%s no file", repo.name, branch)
            return
        changelog_state = repo_state.setdefault("changelog", {})
        changelog_state["path"] = path
        changelog_state["content"] = text
        self._debug(
            "changelog state refreshed repo=%s path=%s content_len=%s",
            repo.name,
            path,
            len(text),
        )

    async def _fetch_changelog(
        self,
        repo: RepoConfig,
        *,
        branch: str,
    ) -> tuple[str | None, str]:
        assert self._client is not None
        candidates = repo.changelog_paths or DEFAULT_CHANGELOG_CANDIDATES
        for path in candidates:
            normalized = path.strip().lstrip("/")
            if not normalized:
                continue
            self._debug(
                "trying changelog repo=%s branch=%s path=%s",
                repo.name,
                branch,
                normalized,
            )
            text = await self._client.get_text_file_if_exists(
                repo.name,
                normalized,
                branch=branch,
            )
            if text is not None:
                self._debug(
                    "found changelog repo=%s branch=%s path=%s content_len=%s",
                    repo.name,
                    branch,
                    normalized,
                    len(text),
                )
                return text, normalized
        return None, ""

    async def _send_commit_notification(
        self,
        repo: RepoConfig,
        notice: CommitNotification,
    ) -> None:
        targets = self._resolve_repo_targets(repo)
        if not targets:
            if not repo.silent_on_empty_target:
                logger.warning("repository %s has no targets", repo.name)
            return
        self._debug(
            "sending commit notification repo=%s target_count=%s commit_count=%s",
            repo.name,
            len(targets),
            len(notice.commits),
        )

        lines = [
            f"GitHub Commit 更新 | {notice.repo_full_name}",
            f"分支: {notice.branch or notice.default_branch}",
            f"新增提交数: {len(notice.commits)}",
            "",
        ]
        for commit in notice.commits:
            sha = str(commit.get("sha", ""))[:7]
            commit_meta = commit.get("commit", {}) or {}
            message = (commit_meta.get("message") or "").strip()
            first_line = message.splitlines()[0] if message else "(no message)"
            author = (
                ((commit_meta.get("author") or {}).get("name")) or
                ((commit.get("author") or {}).get("login")) or
                "unknown"
            )
            url = commit.get("html_url") or f"https://github.com/{notice.repo_full_name}/commit/{commit.get('sha', '')}"
            lines.append(f"- {sha} {first_line}")
            lines.append(f"  作者: {author}")
            lines.append(f"  链接: {url}")

        if notice.compare_url:
            lines.extend(["", f"对比链接: {notice.compare_url}"])

        if notice.changelog_text:
            lines.extend(["", "CHANGELOG 新增:", notice.changelog_text])

        message = "\n".join(lines).strip()
        for target in targets:
            await self._send_chain_to_target(target, self._build_text_chain(target, message))

    async def _send_release_notification(
        self,
        repo: RepoConfig,
        notice: ReleaseNotification,
    ) -> None:
        targets = self._resolve_repo_targets(repo)
        if not targets:
            if not repo.silent_on_empty_target:
                logger.warning("repository %s has no targets", repo.name)
            return
        self._debug(
            "sending release notification repo=%s target_count=%s release=%s",
            repo.name,
            len(targets),
            notice.tag_name or notice.release_name,
        )

        lines = [
            f"GitHub Release 更新 | {notice.repo_full_name}",
            f"名称: {notice.release_name}",
            f"Tag: {notice.tag_name or '(none)'}",
        ]
        if notice.published_at:
            lines.append(f"发布时间: {self._format_iso_datetime(notice.published_at)}")
        lines.append(f"预发布: {'是' if notice.prerelease else '否'}")
        lines.append(f"链接: {notice.html_url}")
        if notice.body:
            lines.extend(["", "Release 说明:", notice.body])
        if notice.changelog_text:
            lines.extend(["", "CHANGELOG 新增:", notice.changelog_text])

        message = "\n".join(lines).strip()
        for target in targets:
            await self._send_chain_to_target(target, self._build_text_chain(target, message))

    def _build_text_chain(self, target: TargetConfig, message: str) -> MessageChain:
        chain = MessageChain()
        if target.mention_all:
            chain.chain.append(AtAll())
            chain.message(" ")
        if target.prefix:
            chain.message(f"{target.prefix}\n")
        chain.message(message)
        return chain

    async def _send_chain_to_target(self, target: TargetConfig, chain: MessageChain) -> bool:
        try:
            plain_text = chain.get_plain_text(with_other_comps_mark=True)
            self._debug(
                "sending message target_umo=%s preview=%s",
                target.umo,
                self._truncate_text(plain_text, 240),
            )
            result = await self.context.send_message(target.umo, chain)
            self._debug(
                "send result target_umo=%s result=%s",
                target.umo,
                result,
            )
            return result
        except Exception:
            logger.exception("failed to send github watch message to target %s", target.umo)
            return False

    def _resolve_repo_targets(self, repo: RepoConfig) -> list[TargetConfig]:
        all_targets = self._load_targets()
        if not repo.target_umos:
            self._debug("repo=%s uses global targets count=%s", repo.name, len(all_targets))
            return all_targets

        target_map = {target.umo: target for target in all_targets}
        results: list[TargetConfig] = []
        for umo in repo.target_umos:
            normalized = self._normalize_umo(umo)
            if not normalized:
                continue
            target = target_map.get(normalized)
            if target:
                results.append(target)
            else:
                results.append(self._target_from_umo(normalized))
        self._debug(
            "repo=%s resolved target_umos=%s matched=%s",
            repo.name,
            repo.target_umos,
            [target.umo for target in results],
        )
        return results

    def _load_targets(self) -> list[TargetConfig]:
        raw_targets = self.config.get("default_targets") or []
        results: list[TargetConfig] = []
        seen: set[str] = set()
        for item in raw_targets:
            if isinstance(item, str):
                umo = self._normalize_umo(item)
                if not umo or umo in seen:
                    continue
                seen.add(umo)
                results.append(TargetConfig(umo=umo))
                continue
            if not isinstance(item, dict):
                continue
            enabled = bool(item.get("enabled", True))
            umo = self._build_umo_from_target(item)
            if not enabled or not umo or umo in seen:
                continue
            seen.add(umo)
            results.append(
                TargetConfig(
                    umo=umo,
                    enabled=enabled,
                    mention_all=bool(item.get("mention_all", False)),
                    prefix=str(item.get("prefix") or "").strip(),
                )
            )
        self._debug("loaded targets count=%s umos=%s", len(results), [item.umo for item in results])
        return results

    def _load_repo_configs(self) -> list[RepoConfig]:
        raw_repos = self.config.get("repositories") or []
        repos: list[RepoConfig] = []
        for item in raw_repos:
            if not isinstance(item, dict):
                continue
            name = self._normalize_repo_name(item.get("name"))
            if not name:
                continue
            paths = self._split_lines(item.get("changelog_paths")) or DEFAULT_CHANGELOG_CANDIDATES.copy()
            target_umos = self._extract_target_umos(item)
            repos.append(
                RepoConfig(
                    name=name,
                    enabled=bool(item.get("enabled", True)),
                    branch=str(item.get("branch") or "").strip(),
                    watch_commits=bool(item.get("watch_commits", True)),
                    watch_releases=bool(item.get("watch_releases", True)),
                    include_commit_diff_url=bool(item.get("include_commit_diff_url", True)),
                    changelog_enabled=bool(item.get("changelog_enabled", True)),
                    changelog_paths=paths,
                    target_umos=target_umos,
                    silent_on_empty_target=bool(item.get("silent_on_empty_target", True)),
                )
            )
        enabled_repos = [repo for repo in repos if repo.enabled]
        self._debug("loaded repos count=%s names=%s", len(enabled_repos), [repo.name for repo in enabled_repos])
        return enabled_repos

    def _extract_target_umos(self, item: dict[str, Any]) -> list[str]:
        direct = self._split_lines(item.get("target_umos"))
        if direct:
            return [umo for umo in (self._normalize_umo(v) for v in direct) if umo]

        # Compatibility with old config styles.
        old_names = set(self._split_lines(item.get("target_names")))
        if old_names:
            matched = []
            for target in self.config.get("default_targets") or []:
                if not isinstance(target, dict):
                    continue
                target_name = str(target.get("name") or "").strip()
                if target_name and target_name in old_names:
                    umo = self._build_umo_from_target(target)
                    if umo:
                        matched.append(umo)
            if matched:
                return matched

        old_target = item.get("target")
        if isinstance(old_target, dict):
            umo = self._build_umo_from_target(old_target)
            if umo:
                return [umo]
        return []

    def _build_umo_from_target(self, item: dict[str, Any]) -> str:
        umo = self._normalize_umo(item.get("umo"))
        if umo:
            return umo
        platform_id = str(item.get("platform_id") or "").strip()
        session_type = str(item.get("session_type") or "").strip()
        session_id = str(item.get("session_id") or "").strip()
        if not (platform_id and session_type and session_id):
            return ""
        try:
            message_type = MessageType(session_type)
        except ValueError:
            return ""
        return str(
            MessageSession(
                platform_name=platform_id,
                message_type=message_type,
                session_id=session_id,
            )
        )

    def _target_from_umo(self, umo: str) -> TargetConfig:
        return TargetConfig(umo=self._normalize_umo(umo))

    def _ensure_target_exists(self, umo: str) -> bool:
        normalized = self._normalize_umo(umo)
        if not normalized:
            return False
        targets = self.config.setdefault("default_targets", [])
        for item in targets:
            if isinstance(item, dict) and self._build_umo_from_target(item) == normalized:
                return False
            if isinstance(item, str) and self._normalize_umo(item) == normalized:
                return False
        targets.append({"umo": normalized, "enabled": True})
        self._debug("added target umo=%s", normalized)
        return True

    def _ensure_repo_subscription(self, repo_name: str, umo: str) -> bool:
        normalized_repo = self._normalize_repo_name(repo_name)
        normalized_umo = self._normalize_umo(umo)
        if not normalized_repo or not normalized_umo:
            return False

        repositories = self.config.setdefault("repositories", [])
        repo_item = None
        for item in repositories:
            if isinstance(item, dict) and self._normalize_repo_name(item.get("name")) == normalized_repo:
                repo_item = item
                break
        if repo_item is None:
            repo_item = {
                "name": normalized_repo,
                "enabled": True,
                "watch_commits": True,
                "watch_releases": True,
                "include_commit_diff_url": True,
                "changelog_enabled": True,
                "changelog_paths": "\n".join(DEFAULT_CHANGELOG_CANDIDATES),
                "target_umos": [],
                "silent_on_empty_target": True,
            }
            repositories.append(repo_item)

        target_umos = self._split_lines(repo_item.get("target_umos"))
        if normalized_umo in [self._normalize_umo(v) for v in target_umos]:
            return False
        target_umos.append(normalized_umo)
        repo_item["target_umos"] = target_umos
        repo_item["enabled"] = True
        self._debug("subscribed repo=%s umo=%s", normalized_repo, normalized_umo)
        return True

    def _remove_repo_subscription(self, repo_name: str, umo: str) -> bool:
        normalized_repo = self._normalize_repo_name(repo_name)
        normalized_umo = self._normalize_umo(umo)
        if not normalized_repo or not normalized_umo:
            return False

        repositories = self.config.get("repositories") or []
        for item in repositories:
            if not isinstance(item, dict):
                continue
            if self._normalize_repo_name(item.get("name")) != normalized_repo:
                continue
            target_umos = self._split_lines(item.get("target_umos"))
            filtered = [v for v in target_umos if self._normalize_umo(v) != normalized_umo]
            if len(filtered) == len(target_umos):
                return False
            item["target_umos"] = filtered
            self._debug("unsubscribed repo=%s umo=%s", normalized_repo, normalized_umo)
            return True
        return False

    def _repos_for_umo(self, umo: str) -> list[str]:
        normalized_umo = self._normalize_umo(umo)
        names: list[str] = []
        for repo in self._load_repo_configs():
            if normalized_umo in [self._normalize_umo(v) for v in repo.target_umos]:
                names.append(repo.name)
        return names

    async def _load_state(self) -> None:
        async with self._state_lock:
            if not self._state_file.exists():
                self._state = {"repos": {}, "subscriptions": {}}
                self._debug("state file not found, using empty state path=%s", self._state_file)
                return
            try:
                self._state = json.loads(self._state_file.read_text(encoding="utf-8"))
                self._state.setdefault("repos", {})
                self._state.setdefault("subscriptions", {})
                self._debug(
                    "state loaded path=%s repo_count=%s",
                    self._state_file,
                    len(self._state.get("repos", {})),
                )
            except Exception:
                logger.exception("failed to load github watch state, resetting")
                self._state = {"repos": {}, "subscriptions": {}}

    async def _save_state(self) -> None:
        async with self._state_lock:
            self._state_file.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._debug(
                "state saved path=%s repo_count=%s",
                self._state_file,
                len(self._state.get("repos", {})),
            )

    def _get_repo_state(self, repo_full_name: str) -> dict[str, Any]:
        repos = self._state.setdefault("repos", {})
        return repos.setdefault(repo_full_name, {})

    def _normalize_repo_name(self, value: Any) -> str:
        text = str(value or "").strip()
        text = text.removeprefix("https://github.com/")
        text = text.removeprefix("http://github.com/")
        text = text.strip("/")
        if "/tree/" in text:
            text = text.split("/tree/", 1)[0]
        if "/blob/" in text:
            text = text.split("/blob/", 1)[0]
        parts = [part for part in text.split("/") if part]
        if len(parts) < 2:
            return ""
        return f"{parts[0]}/{parts[1]}"

    def _normalize_umo(self, value: Any) -> str:
        text = str(value or "").strip()
        if text.count(":") < 2:
            return ""
        return text

    def _extract_added_prefix(self, old_text: str, new_text: str) -> str:
        if new_text == old_text:
            return ""
        if new_text.endswith(old_text):
            return new_text[: len(new_text) - len(old_text)]
        old_lines = old_text.splitlines()
        new_lines = new_text.splitlines()
        idx = 0
        max_len = min(len(old_lines), len(new_lines))
        while idx < max_len and old_lines[-(idx + 1)] == new_lines[-(idx + 1)]:
            idx += 1
        if idx == 0:
            return new_text
        if idx >= len(new_lines):
            return ""
        return "\n".join(new_lines[: len(new_lines) - idx])

    def _truncate_text(self, text: str, max_length: int) -> str:
        normalized = self._normalize_text(text)
        if len(normalized) <= max_length:
            return normalized
        return normalized[: max(0, max_length - 3)].rstrip() + "..."

    def _normalize_text(self, text: str) -> str:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _format_iso_datetime(self, value: str) -> str:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            return value

    def _extract_command_args(self, message: str, command_names: list[str]) -> str:
        pattern = r"^/?(?:" + "|".join(re.escape(name) for name in command_names) + r")\s*"
        return re.sub(pattern, "", message or "", count=1, flags=re.IGNORECASE).strip()

    def _split_lines(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value or "")
        return [line.strip() for line in text.splitlines() if line.strip()]

    def _format_http_error(self, exc: httpx.HTTPStatusError) -> str:
        status = exc.response.status_code
        if status == 401:
            return "GitHub Token 无效或未授权"
        if status == 403:
            return "GitHub API 被限流或无权限访问"
        if status == 404:
            return "仓库或资源不存在"
        return f"GitHub API 错误 {status}"

    def _log_http_status_error(self, repo_name: str, exc: httpx.HTTPStatusError) -> None:
        if not self._debug_enabled():
            return
        request = exc.request
        response = exc.response
        logger.error(
            "[github_repo_watch] http status error repo=%s method=%s url=%s status=%s body=%s",
            repo_name,
            request.method if request else "UNKNOWN",
            str(request.url) if request else "UNKNOWN",
            response.status_code if response else "UNKNOWN",
            self._truncate_text(response.text if response else "", 1200),
        )

    def _poll_interval_seconds(self) -> int:
        return max(60, self._int("poll_interval_seconds", DEFAULT_POLL_INTERVAL))

    def _int(self, key: str, default: int) -> int:
        value = self.config.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _text(self, key: str, default: str) -> str:
        value = self.config.get(key, default)
        if value is None:
            return default
        return str(value)

    def _bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _debug_enabled(self) -> bool:
        if "debug_mode" in self.config:
            return self._bool("debug_mode", False)
        return self._bool("debug_logging_enabled", False)

    def _debug(self, message: str, *args: Any) -> None:
        if self._debug_enabled():
            logger.info("[github_repo_watch] " + message, *args)
