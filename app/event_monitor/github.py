from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from .storage import Store, now_iso

LOG = logging.getLogger(__name__)
REPO_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class GitHubError(RuntimeError):
    pass


class GitHubBacklogError(GitHubError):
    pass


class GitHubRateLimitError(GitHubError):
    def __init__(self, message: str, *, reset_at: str | None = None,
                 retry_after_seconds: int | None = None):
        super().__init__(message)
        self.reset_at = reset_at
        self.retry_after_seconds = retry_after_seconds


class GitHubClient:
    def __init__(self, token: str | None = None, timeout: float = 15.0):
        self.token = (token or os.getenv("GITHUB_TOKEN", "")).strip() or None
        self.timeout = timeout
        self.rate_limit: dict[str, Any] = {
            "limit": None,
            "remaining": None,
            "used": None,
            "reset_at": None,
            "resource": None,
            "observed_at": None,
        }

    def get(self, path: str) -> Any:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return self._get_once(path)
            except GitHubError as exc:
                if "unavailable" not in str(exc).casefold():
                    raise
                last_error = exc
                if attempt == 0:
                    time.sleep(0.8)
        raise GitHubError("GitHub API 暂时不可用：连接超时，已自动重试") from last_error

    def _get_once(self, path: str) -> Any:
        url = path if path.startswith("https://") else "https://api.github.com" + path
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "bittensor-event-monitor/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                self._capture_rate_limit(response.headers)
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read(1000).decode("utf-8", errors="replace")
            self._capture_rate_limit(exc.headers)
            remaining = exc.headers.get("X-RateLimit-Remaining")
            reset_epoch = exc.headers.get("X-RateLimit-Reset")
            retry_after = exc.headers.get("Retry-After")
            lowered = body.casefold()
            is_rate_limit = (
                exc.code in (403, 429)
                and (
                    remaining == "0"
                    or bool(retry_after)
                    or "rate limit" in lowered
                    or "secondary rate limit" in lowered
                    or "abuse detection" in lowered
                )
            )
            if is_rate_limit:
                reset_at = None
                try:
                    reset_at = datetime.fromtimestamp(
                        int(reset_epoch), timezone.utc
                    ).isoformat(timespec="seconds").replace("+00:00", "Z")
                except (TypeError, ValueError, OSError):
                    pass
                retry_seconds = None
                try:
                    retry_seconds = max(0, int(retry_after))
                except (TypeError, ValueError):
                    pass
                raise GitHubRateLimitError(
                    "GitHub API 已达到速率限制",
                    reset_at=reset_at,
                    retry_after_seconds=retry_seconds,
                ) from exc
            raise GitHubError(f"GitHub API HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GitHubError(f"GitHub API unavailable: {exc}") from exc

    def _capture_rate_limit(self, headers: Any) -> None:
        def integer(name: str) -> int | None:
            try:
                value = headers.get(name)
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        limit = integer("X-RateLimit-Limit")
        remaining = integer("X-RateLimit-Remaining")
        used = integer("X-RateLimit-Used")
        reset_epoch = integer("X-RateLimit-Reset")
        if limit is None and remaining is None and used is None and reset_epoch is None:
            return
        reset_at = None
        if reset_epoch is not None:
            try:
                reset_at = datetime.fromtimestamp(
                    reset_epoch, timezone.utc
                ).isoformat(timespec="seconds").replace("+00:00", "Z")
            except (ValueError, OSError):
                pass
        if used is None and limit is not None and remaining is not None:
            used = max(0, limit - remaining)
        self.rate_limit = {
            "limit": limit,
            "remaining": remaining,
            "used": used,
            "reset_at": reset_at,
            "resource": str(headers.get("X-RateLimit-Resource") or "core"),
            "observed_at": now_iso(),
        }


class GitHubCollector:
    def __init__(self, *, store: Store, publish: Any, status: Any,
                 client: GitHubClient | None = None, interval_seconds: int = 300,
                 max_repositories_per_poll: int | None = None,
                 max_workers: int = 3,
                 network: str = "finney"):
        self.store = store
        self.publish = publish
        self.status = status
        self.network = network
        self.client = client or GitHubClient()
        self.interval_seconds = max(60, interval_seconds)
        self.max_repositories_per_poll = max_repositories_per_poll or (
            30 if getattr(self.client, "token", None) else 1
        )
        self.max_workers = max(1, min(8, int(max_workers)))

    def update_token(self, token: str | None) -> None:
        normalized = (token or "").strip() or None
        self.client.token = normalized
        self.max_repositories_per_poll = 30 if normalized else 1

    def _repo_path(self, repo: dict[str, Any], suffix: str) -> str:
        owner = urllib.parse.quote(repo["owner"], safe="")
        name = urllib.parse.quote(repo["name"], safe="")
        return f"/repos/{owner}/{name}{suffix}"

    def _metadata(self, repo: dict[str, Any]) -> dict[str, Any]:
        meta = self.client.get(self._repo_path(repo, ""))
        if not isinstance(meta, dict):
            raise GitHubError("repository metadata response was not an object")
        return meta

    def _commits(self, repo: dict[str, Any], branch: str,
                 cursor: str | None = None) -> list[dict[str, Any]]:
        if not cursor:
            query = urllib.parse.urlencode({"sha": branch, "per_page": 1, "page": 1})
            data = self.client.get(self._repo_path(repo, "/commits?") + query)
            return data if isinstance(data, list) else []
        query = urllib.parse.urlencode({"sha": branch, "per_page": 100, "page": 1})
        data = self.client.get(self._repo_path(repo, "/commits?") + query)
        batch = data if isinstance(data, list) else []
        if any(str(item.get("sha")) == cursor for item in batch):
            return batch
        raise GitHubBacklogError("GitHub 提交游标不在最新 100 条内")

    def _releases(self, repo: dict[str, Any], cursor: str | None = None) -> list[dict[str, Any]]:
        data = self.client.get(self._repo_path(repo, "/releases?per_page=30&page=1"))
        return data if isinstance(data, list) else []

    def _commit_event(self, repo: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        commit = item.get("commit") or {}
        sha = str(item.get("sha", ""))
        author = commit.get("author") or {}
        date = author.get("date") or now_iso()
        detail: dict[str, Any] = {}
        if sha and getattr(self.client, "token", None):
            try:
                fetched = self.client.get(self._repo_path(repo, f"/commits/{urllib.parse.quote(sha, safe='')}"))
                if isinstance(fetched, dict):
                    detail = fetched
            except GitHubRateLimitError:
                raise
            except GitHubError as exc:
                LOG.warning("GitHub commit detail unavailable for %s: %s", sha, exc)
        stats = detail.get("stats") or {}
        additions = int(stats.get("additions") or 0)
        deletions = int(stats.get("deletions") or 0)
        changes = additions + deletions
        files = detail.get("files") if isinstance(detail.get("files"), list) else []
        file_names = [str(item.get("filename") or "") for item in files if isinstance(item, dict)]
        file_count = len(file_names)
        full_message = str(commit.get("message", ""))
        change_kind = _commit_change_kind(full_message, file_names)
        rating = _commit_update_rating(changes, file_count)
        change_brief = _commit_change_brief(full_message, file_names, change_kind)
        progress_points = _commit_progress_points(full_message, files, change_kind)
        if detail:
            summary = f"SN{repo['netuid']} GitHub {change_brief} · 评级：{rating}"
        else:
            summary = f"SN{repo['netuid']} GitHub {change_brief} · 评级：待确认"
        event_id = hashlib.sha256(f"github:{repo['repository_id']}:commit:{sha}".encode()).hexdigest()
        return {
            "event_id": event_id,
            "network": repo["network"], "netuid": repo["netuid"],
            "event_type": "GitHub 仓库更新",
            "event_name": "GitHub.DefaultBranchCommit",
            "source": "github", "source_url": item.get("html_url"), "event_at": date,
            "summary": summary,
            "details": {
                "kind": "commit", "sha": sha, "branch": repo.get("default_branch"),
                "message": commit.get("message"), "author": author.get("name") or "",
                "repository": f"{repo['owner']}/{repo['name']}",
                "change_kind": change_kind, "change_brief": change_brief,
                "progress_points": progress_points,
                "additions": additions, "deletions": deletions, "changes": changes,
                "files_changed": file_count, "update_rating": rating,
                "files_preview": file_names[:10],
            },
            "raw_data": {"list_item": item, "detail": detail}, "chain_status": None,
            "enrichment_status": "complete", "confidence": "high",
        }

    def _release_event(self, repo: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        release_id = str(item.get("id", item.get("tag_name", "")))
        tag = str(item.get("tag_name", ""))
        name = str(item.get("name") or tag or "未命名发布")
        event_id = hashlib.sha256(f"github:{repo['repository_id']}:release:{release_id}".encode()).hexdigest()
        return {
            "event_id": event_id,
            "network": repo["network"], "netuid": repo["netuid"],
            "event_type": "GitHub 发布",
            "event_name": "GitHub.ReleasePublished",
            "source": "github", "source_url": item.get("html_url"),
            "event_at": item.get("published_at") or item.get("created_at") or now_iso(),
            "summary": f"SN{repo['netuid']} GitHub 正式发布：{name}",
            "details": {
                "kind": "release", "tag": tag, "name": name,
                "prerelease": bool(item.get("prerelease")),
                "repository": f"{repo['owner']}/{repo['name']}",
            },
            "raw_data": item, "chain_status": None,
            "enrichment_status": "complete", "confidence": "high",
        }

    def poll_repository(self, repo: dict[str, Any]) -> list[dict[str, Any]]:
        branch = str(repo.get("default_branch") or "")
        if not branch:
            meta = self._metadata(repo)
            branch = str(meta.get("default_branch") or "main")
        commit_cursor = repo.get("commit_cursor")
        release_cursor = repo.get("release_cursor")
        try:
            commits = self._commits(repo, branch, commit_cursor)
        except GitHubBacklogError:
            latest = self.client.get(
                self._repo_path(repo, "/commits?")
                + urllib.parse.urlencode({"sha": branch, "per_page": 1, "page": 1})
            )
            latest_sha = (
                str(latest[0].get("sha"))
                if isinstance(latest, list) and latest and latest[0].get("sha")
                else "none"
            )
            self.store.reset_repo_baseline(
                repo["repository_id"], branch, latest_sha,
            )
            self.store.add_log(
                "WARNING",
                "app.github",
                f"仓库 {repo['owner']}/{repo['name']} 提交积压超过 1000 条，已从最新提交重建基线",
            )
            return []
        releases = self._releases(repo, release_cursor)

        if not repo.get("last_checked_at"):
            self.store.set_repo_baseline(
                repo["repository_id"], branch,
                str(commits[0].get("sha")) if commits else "none",
                str(releases[0].get("id")) if releases else "none",
            )
            return []

        new_commits: list[dict[str, Any]] = []
        for item in commits:
            sha = str(item.get("sha", ""))
            if sha and commit_cursor and sha == commit_cursor:
                break
            if sha:
                new_commits.append(self._commit_event(repo, item))

        new_releases: list[dict[str, Any]] = []
        for item in releases:
            release_id = str(item.get("id", item.get("tag_name", "")))
            if release_cursor and release_id == release_cursor:
                break
            if not item.get("draft"):
                new_releases.append(self._release_event(repo, item))

        events = sorted(new_releases + new_commits, key=lambda item: item["event_at"])
        created = []
        for event in events:
            stored, was_created = self.store.create_event(event)
            if was_created:
                created.append(stored)
                self.publish(stored, "discovered")
        self.store.advance_repo_cursor(
            repo["repository_id"],
            commit=str(commits[0].get("sha")) if commits else commit_cursor,
            release=str(releases[0].get("id")) if releases else release_cursor,
            branch=branch,
        )
        return created

    def poll_once(self) -> dict[str, Any]:
        repos = self.store.list_polling_repositories(self.network)
        queued_total = len(repos)
        repos = repos[:self.max_repositories_per_poll]
        started = time.monotonic()
        failures = 0
        successes = 0
        attempted = 0
        rate_limited = False
        rate_limited_repository: str | None = None
        rate_limit_reset_at: str | None = None
        retry_after_seconds: int | None = None
        created = 0
        self.status("github", "running", {
            "message": "正在轮询 GitHub 仓库",
            "repositories": len(repos),
            "repository_queue_total": queued_total,
            "workers": min(self.max_workers, max(1, len(repos))),
            "started_at": now_iso(),
        })
        pending = iter(repos)
        in_flight: dict[Any, dict[str, Any]] = {}
        worker_count = min(self.max_workers, max(1, len(repos)))

        def submit_next(executor: ThreadPoolExecutor) -> None:
            if rate_limited or len(in_flight) >= worker_count:
                return
            try:
                repo = next(pending)
            except StopIteration:
                return
            in_flight[executor.submit(self.poll_repository, repo)] = repo

        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="github-poll") as executor:
            for _ in range(worker_count):
                submit_next(executor)
            while in_flight:
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in done:
                    repo = in_flight.pop(future)
                    attempted += 1
                    repo_component = f"github:{repo['owner']}/{repo['name']}"
                    try:
                        created += len(future.result())
                        successes += 1
                        self.store.resolve_alerts(repo_component)
                    except Exception as exc:
                        LOG.exception("GitHub poll failed for %s/%s", repo["owner"], repo["name"])
                        self.store.advance_repo_cursor(repo["repository_id"], error=str(exc)[:400])
                        self.store.add_alert(
                            repo_component, "warning",
                            f"仓库 {repo['owner']}/{repo['name']} 轮询失败：{exc}"[:400],
                        )
                        if isinstance(exc, GitHubRateLimitError) or "rate limit" in str(exc).casefold():
                            rate_limited = True
                            rate_limited_repository = f"{repo['owner']}/{repo['name']}"
                            rate_limit_reset_at = getattr(exc, "reset_at", None)
                            retry_after_seconds = getattr(exc, "retry_after_seconds", None)
                        else:
                            failures += 1
                while not rate_limited and len(in_flight) < worker_count:
                    before = len(in_flight)
                    submit_next(executor)
                    if len(in_flight) == before:
                        break

        if successes == attempted:
            self.store.resolve_alerts("github")
        if rate_limited:
            state = "limited"
        elif failures and successes == 0:
            state = "degraded"
        elif failures:
            state = "partial"
        else:
            state = "connected"
        summary = {
            "state": state,
            "repositories": attempted,
            "repositories_queued": len(repos) - attempted,
            "repository_queue_total": queued_total,
            "events_created": created,
            "failures": failures,
            "successes": successes,
            "workers": worker_count,
            "rate_limited": rate_limited,
            "rate_limited_repository": rate_limited_repository,
            "rate_limit_reset_at": rate_limit_reset_at,
            "retry_after_seconds": retry_after_seconds,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "updated_at": now_iso(),
            "api_rate_limit": getattr(self.client, "rate_limit", None),
        }
        self.status("github", state, summary)
        return summary

    def run(self, stop: Any) -> None:
        # 启动时清掉上一轮未解决的 github 告警。这里若数据库短暂只读（多实例重启、
        # WAL 文件被扫描工具短暂占用等）会抛 OperationalError，历史上曾让整个
        # github 采集线程在第一行就崩溃退出、再也不会重试，导致 GitHub 事件流彻底
        # 停摆。与 chain.run 一致，把启动清理放进容错里：失败不致命，下轮重试。
        try:
            self.store.resolve_alert_family("github:")
            self.store.resolve_alerts("github")
        except Exception:
            LOG.exception("GitHub collector startup cleanup failed (will retry)")
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                LOG.exception("GitHub collector error")
                self.status("github", "degraded", {"error": str(exc), "updated_at": now_iso()})
                try:
                    self.store.add_alert("github", "critical", f"GitHub 监听失败：{exc}"[:400])
                except Exception:
                    pass
            stop.wait(self.interval_seconds)


COMMIT_TOPIC_PATTERNS: tuple[tuple[str, str], ...] = (
    ("validator reset", "验证者重置建议"),
    ("one card", "单卡 GPU 重置"),
    ("nvidia-smi", "nvidia-smi 单卡定位"),
    ("port batch", "端口批次探测"),
    ("late listeners", "延迟监听端口重试"),
    ("audit backlog", "审计积压统计"),
    ("waiting for their draw", "等待抽签的记录"),
    ("waiting for draw", "等待抽签的记录"),
    ("sampled job", "抽样任务"),
    ("admin job status", "管理任务状态"),
    ("miners were paid", "矿工分期收益"),
    ("judge source pages", "评审源页面"),
    ("privacy redaction", "隐私脱敏"),
    ("leader generation", "主节点生成"),
    ("async miner runtime", "异步矿工运行时"),
    ("worker image", "任务镜像"),
    ("witness submissions", "见证提交"),
    ("manual cli", "手动命令行"),
    ("weights", "权重处理"),
    ("on chain", "链上提交"),
    ("evaluation", "评估流程"),
    ("background loop", "后台循环"),
    ("openrouter budgets", "OpenRouter 预算"),
    ("account restrictions", "账户限制说明"),
    ("rented pool", "GPU 租赁池"),
    ("rental", "租赁费用"),
    ("saved partial feedback", "失败评审的部分反馈"),
    ("failed judge attempts", "失败评审重试"),
    ("chute failed", "Chute 失败上限"),
    ("health check", "健康检查"),
    ("cache", "缓存"),
    ("database", "数据库"),
    ("release", "发布流程"),
    ("api", "API 接口"),
    ("dashboard", "管理面板"),
    ("scoring", "评分逻辑"),
    ("training", "训练流程"),
)

COMMIT_AREA_PATTERNS: tuple[tuple[str, str], ...] = (
    ("validator", "验证者模块"),
    ("miner", "矿工模块"),
    ("neuron", "神经元模块"),
    ("scoring", "评分模块"),
    ("training", "训练模块"),
    ("service", "服务接口"),
    ("server", "服务接口"),
    ("api", "API 接口"),
    ("database", "数据库"),
    ("sql", "数据库"),
    ("config", "配置"),
    ("cli", "命令行"),
    ("ui", "界面"),
    ("dashboard", "管理面板"),
    ("chain", "链上交互"),
)


def _commit_message_text(message: str) -> str:
    lines = [line.strip() for line in str(message or "").splitlines()]
    lines = [
        line for line in lines
        if line
        and not line.casefold().startswith(("co-authored-by:", "signed-off-by:"))
        and not line.startswith(("Merge pull request", "Merge remote-tracking", "Merge branch", "Merge commit"))
    ]
    if not lines:
        return "无提交说明"
    text = lines[0]
    text = re.sub(
        r"^(fix|feat|feature|refactor|perf|docs?|test|build|chore)(\([^)]*\))?:\s*",
        "", text, flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+\(#[0-9]+\)\s*$", "", text)
    text = re.sub(r"\s+\[[A-Za-z]+-\d+\]\s*", " ", text)
    return text.strip()[:220] or "无提交说明"


def _commit_topics(message: str, file_names: list[str]) -> list[str]:
    lowered = str(message or "").casefold()
    topics: list[str] = []
    for needle, label in COMMIT_TOPIC_PATTERNS:
        if re.search(rf"\b{re.escape(needle)}\b", lowered) and label not in topics:
            topics.append(label)
        if len(topics) >= 4:
            break
    if topics:
        return topics
    lowered_files = " ".join(file_names).casefold()
    areas: list[str] = []
    for needle, label in COMMIT_AREA_PATTERNS:
        if needle in lowered_files and label not in areas:
            areas.append(label)
        if len(areas) >= 3:
            break
    return areas or ["代码与配置"]


def _commit_change_brief(message: str, file_names: list[str], change_kind: str) -> str:
    text = _commit_message_text(message)
    if re.search(r"[\u4e00-\u9fff]", text):
        return f"更新说明：{text[:120]}"
    topics = _commit_topics(message, file_names)
    action = {
        "问题修复": "修复",
        "功能更新": "新增与改进",
        "代码重构": "调整",
        "文档更新": "文档更新",
        "测试更新": "测试更新",
        "构建配置更新": "构建配置更新",
        "代码更新": "更新",
    }.get(change_kind, "更新")
    if action in {"文档更新", "测试更新", "构建配置更新"}:
        return action
    return f"{action}：{'、'.join(topics)}"


def _is_test_file(name: str) -> bool:
    lowered = name.casefold()
    return "test" in lowered or lowered.startswith(("tests/", "test/"))


def _is_document_file(name: str) -> bool:
    lowered = name.casefold()
    return lowered.endswith((".md", ".mdx", ".rst", ".txt", ".adoc")) or bool(
        re.search(r"(^|/)docs?/", lowered)
    )


def _is_config_file(name: str) -> bool:
    base = PurePosixPath(name).name.casefold()
    return (
        base in {
            "pyproject.toml", "requirements.txt", "package.json", "package-lock.json",
            "cargo.toml", "go.mod", "dockerfile", "docker-compose.yml", "makefile",
        }
        or name.casefold().startswith((".github/workflows/", "config/", "configs/"))
        or base.endswith((".yaml", ".yml", ".toml", ".ini", ".env.example"))
    )


def _commit_progress_points(message: str, files: list[dict[str, Any]], change_kind: str) -> list[str]:
    file_names = [str(item.get("filename") or "") for item in files if isinstance(item, dict)]
    topics = _commit_topics(message, file_names)
    code_files = [name for name in file_names if not _is_test_file(name) and not _is_document_file(name) and not _is_config_file(name)]
    test_files = [name for name in file_names if _is_test_file(name)]
    doc_files = [name for name in file_names if _is_document_file(name)]
    config_files = [name for name in file_names if _is_config_file(name)]
    points: list[str] = []
    if topics and topics != ["代码与配置"]:
        points.append(f"本次重点：{'、'.join(topics)}。")
    elif change_kind != "代码更新":
        points.append(f"本次更新类型：{change_kind}。")

    symbols: list[str] = []
    doc_headings: list[str] = []
    for file in files:
        if not isinstance(file, dict):
            continue
        filename = str(file.get("filename") or "")
        if _is_test_file(filename):
            continue
        patch = str(file.get("patch") or "")
        for line in patch.splitlines():
            if not line.startswith("+"):
                continue
            added = line[1:].strip()
            match = re.match(r"(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)", added)
            if not match:
                match = re.match(r"class\s+([A-Za-z_][A-Za-z0-9_]*)", added)
            if not match:
                match = re.match(r"(?:pub\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)", added)
            if not match:
                match = re.match(r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)", added)
            if match and match.group(1) not in symbols:
                symbols.append(match.group(1))
            if _is_document_file(str(file.get("filename") or "")):
                heading = re.match(r"#{1,4}\s+(.+)", added)
                if heading and heading.group(1) not in doc_headings:
                    doc_headings.append(heading.group(1)[:80])

    if code_files:
        area = _commit_topics("", code_files)
        point = f"代码改动：涉及 {len(code_files)} 个文件"
        if area != ["代码与配置"]:
            point += f"，集中在{'、'.join(area)}"
        points.append(point + "。")
    if symbols:
        points.append(f"新增或调整的代码入口：{'、'.join(symbols[:8])}。")
    if test_files:
        points.append(f"测试变化：新增或调整 {len(test_files)} 个测试文件。")
    if doc_files:
        point = f"文档更新：{len(doc_files)} 个文件"
        if doc_headings:
            point += f"，涉及章节“{'”“'.join(doc_headings[:4])}”"
        points.append(point + "。")
    if config_files:
        points.append(f"配置变化：调整 {len(config_files)} 个构建、依赖或运行配置文件。")
    if not points:
        points.append("本次提交未返回可解析的文件级改动；请结合原始提交说明和 GitHub Diff 核验。")
    return points[:7]


def _commit_change_kind(message: str, file_names: list[str]) -> str:
    lowered_message = message.casefold()
    lowered_files = [name.casefold() for name in file_names if name]
    if lowered_files and all(
        name.endswith((".md", ".rst", ".txt"))
        or name.startswith(("docs/", "documentation/"))
        for name in lowered_files
    ):
        return "文档更新"
    if lowered_files and all("test" in name or name.endswith("_test.py") for name in lowered_files):
        return "测试更新"
    if any(name.startswith((".github/workflows/", "docker")) or name in {"pyproject.toml", "requirements.txt", "package.json"} for name in lowered_files):
        return "构建配置更新"
    if lowered_message.startswith(("fix", "bugfix", "hotfix")) or any(word in lowered_message for word in (" fix ", " bug ")):
        return "问题修复"
    if lowered_message.startswith(("feat", "feature")) or any(word in lowered_message for word in (" add ", " new ")):
        return "功能更新"
    if lowered_message.startswith(("refactor", "perf")):
        return "代码重构"
    return "代码更新"


def _commit_update_rating(changes: int, files_changed: int) -> str:
    if changes <= 20 and files_changed <= 2:
        return "小"
    if changes <= 150 and files_changed <= 8:
        return "中"
    if changes <= 800 and files_changed <= 30:
        return "大"
    return "重大"


def parse_github_repository(value: Any) -> tuple[str, str] | None:
    """Extract owner/name from a chain identity value or a GitHub URL."""
    text = _text_value(value).strip()
    if not text:
        return None
    if text.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(text)
        if (parsed.hostname or "").casefold() not in {"github.com", "www.github.com"}:
            return None
        parts = [part for part in parsed.path.split("/") if part]
    else:
        parts = [part for part in text.split("/") if part]
    if len(parts) < 2:
        return None
    owner, name = parts[0], parts[1]
    if owner.casefold() in {
        "about", "collections", "events", "features", "login", "marketplace",
        "notifications", "orgs", "pricing", "search", "settings", "sponsors",
        "topics", "users",
    }:
        return None
    if name.casefold().endswith(".git"):
        name = name[:-4]
    if not REPO_PART.fullmatch(owner) or not REPO_PART.fullmatch(name):
        return None
    return owner, name


def _text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        if value.startswith("0x"):
            try:
                return bytes.fromhex(value[2:]).decode("utf-8", errors="replace")
            except ValueError:
                return value
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (list, tuple)) and all(isinstance(item, int) for item in value):
        try:
            return bytes(value).decode("utf-8", errors="replace")
        except ValueError:
            return ""
    if isinstance(value, dict):
        return ""
    return str(value)
