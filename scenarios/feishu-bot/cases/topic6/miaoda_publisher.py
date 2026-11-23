"""Gateway 侧妙搭发布兜底（不可抗力时的 backup，不替代 Agent 的 Phase H）。

只在 Agent 的 Phase H 因环境故障（沙箱用户凭据失效、沙箱不可用）失败、且用户手动发送
/republish 时使用。发布动作与 miaoda-web-publish skill 的 Git 管理流程一致：
validate_html → (+create) → +init → 提交 index.html → push sprint/default →
+release-create → +release-get 轮询 → 取 API 返回的 online_url。

安全约束：
- 用户 token 只经子进程环境变量传给 lark-cli，不写文件、不进日志、不出现在异常文本里。
- 每次在独立临时目录（独立 HOME）里执行，结束即删除。
- 同名应用且不能按历史 online_url 精确确认归属时停止，不模糊覆盖（skill 规则）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

log = logging.getLogger("arkagent.topic6.miaoda_fallback")

CASE_DIR = Path(__file__).resolve().parent
VALIDATE_HTML = CASE_DIR / "ma-resources/skills/miaoda-web-publish/scripts/validate_html.py"
RELEASE_BRANCH = "sprint/default"
RELEASE_POLL_INTERVAL_SEC = 5.0
RELEASE_POLL_TIMEOUT_SEC = 300.0
CLI_TIMEOUT_SEC = 300
LIST_PAGE_SIZE = 50
LIST_MAX_PAGES = 20
# 透传给子进程的非敏感环境（代理/语言），其余一律不继承宿主环境。
_PASSTHROUGH_ENV = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
)
_WEB_SOURCE_RE = re.compile(r"(?:^|/)08_UI发布/(热点报告_(.+?))_web/index\.html$")


class MiaodaFallbackError(RuntimeError):
    """兜底发布失败；文本可直接发群（不含 token）。"""


@dataclass(frozen=True)
class WebArtifact:
    path: Path          # 212 本地已同步的 index.html
    sha256: str
    app_name: str       # 热点报告_{period_label}，与 prompt 09 规则一致
    source: str         # 沙箱内相对路径（展示用）
    published_at: str


@dataclass(frozen=True)
class PublishResult:
    app_id: str
    release_id: str
    online_url: str
    operation: str      # create | update
    sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def locate_web_artifact(
    job_root: Path, min_published_at: str = "", round_sha256s: frozenset = frozenset()
) -> WebArtifact:
    """按 artifact_sync 落盘的 index.json + 最新 manifest 找本轮 Phase G 的 index.html，并核 sha256。

    manifest 是 Agent 自己声明的发布清单（publish_node_artifacts.py 写入），取 node=web 最近一次
    publication 的 index.html 条目；只认 sha256 一致的本地对象，对不上就拒绝发布（防旧版本）。
    """
    index_path = job_root / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        files = [f for f in index["files"].values() if isinstance(f, dict)]
    except (OSError, KeyError, ValueError, AttributeError) as error:
        raise MiaodaFallbackError(f"未找到已同步的任务产物（{index_path.name}）：{error!s}"[:200]) from error

    best: Optional[tuple[str, dict]] = None
    for item in files:
        if Path(str(item.get("filename") or "")).name != "manifest.json":
            continue
        try:
            manifest = json.loads((job_root / str(item["object_path"])).read_text(encoding="utf-8"))
        except (OSError, KeyError, ValueError):
            continue
        for publication in manifest.get("publications") or []:
            if not isinstance(publication, dict) or publication.get("node") != "web":
                continue
            if publication.get("status") not in (None, "", "complete"):
                continue
            for artifact in publication.get("files") or []:
                source = str((artifact or {}).get("source") or "")
                if not _WEB_SOURCE_RE.search(source):
                    continue
                stamp = str(publication.get("published_at") or "")
                if best is None or stamp > best[0]:
                    best = (stamp, {**artifact, "published_at": stamp})
    if best is None:
        raise MiaodaFallbackError("产物清单里没有 Phase G 发布的网页（node=web 的 index.html），无法兜底发布。")
    # 本轮重建结果与上轮逐字节相同时 Agent 不会重新发布 manifest：发布时间早于本轮，
    # 但 sha256 出现在本轮工具输出里（Agent 自己打印的 index.html 哈希）即视为本轮产物。
    if (min_published_at and best[0] < min_published_at
            and str(best[1].get("sha256") or "") not in round_sha256s):
        raise MiaodaFallbackError(
            "本轮 Phase G 还没有生成新网页（最近一次网页产物早于本次 /republish），未发布旧版本。"
        )

    artifact = best[1]
    expected = str(artifact.get("sha256") or "")
    source = str(artifact.get("source") or "")
    match = _WEB_SOURCE_RE.search(source)
    if match is None:  # pragma: no cover - 上面已按同一正则筛选
        raise MiaodaFallbackError("网页产物路径不符合约定")
    for item in files:
        object_path = job_root / str(item.get("object_path") or "")
        if Path(str(item.get("filename") or "")).name != "index.html" or not object_path.is_file():
            continue
        if expected and _sha256(object_path) == expected:
            return WebArtifact(object_path, expected, match.group(1), source, artifact["published_at"])
    raise MiaodaFallbackError(
        f"已同步的网页与产物清单不一致（期望 sha256 {expected[:12]}…），可能尚未同步完成，请稍后再试。"
    )


class MiaodaPublisher:
    def __init__(
        self,
        *,
        cli_bin: str,
        app_id: str,
        node_bin_dir: str = "",
        work_root: Optional[Path] = None,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._cli = cli_bin
        self._app_id = app_id
        self._node_bin_dir = node_bin_dir
        self._work_root = work_root
        self._run = runner
        self._sleep = sleep

    @property
    def available(self) -> bool:
        return bool(self._cli) and Path(self._cli).is_file() and os.access(self._cli, os.X_OK)

    # ---- 对外入口（同步，调用方放 executor 里跑） ----

    def publish(
        self,
        artifact: WebArtifact,
        access_token: str,
        *,
        known_online_urls: Sequence[str] = (),
    ) -> PublishResult:
        if not self.available:
            raise MiaodaFallbackError("Gateway 未配置可用的 lark-cli（TOPIC6_MIAODA_CLI_BIN），无法兜底发布。")
        if _sha256(artifact.path) != artifact.sha256:
            raise MiaodaFallbackError("网页文件校验失败（sha256 已变化），已停止发布。")
        workdir = Path(tempfile.mkdtemp(prefix="miaoda_fallback_", dir=self._work_root))
        try:
            (workdir / "home").mkdir()
            env = self._env(workdir / "home", access_token)
            self._validate(artifact)
            app_id, operation = self._resolve_target(env, artifact, known_online_urls)
            repo = workdir / "repo"
            self._cli_json(env, "+init", "--app-id", app_id, "--dir", str(repo), step="+init", app_id=app_id)
            self._commit(env, repo, artifact)
            self._push(env, repo, app_id)
            release = self._cli_json(
                env, "+release-create", "--app-id", app_id, "--branch", RELEASE_BRANCH,
                step="+release-create", app_id=app_id,
            )
            release_id = str(_dig(release, "release_id") or "")
            if not release_id:
                raise MiaodaFallbackError(f"+release-create 未返回 release_id（app_id={app_id}）")
            online_url = self._wait_release(env, app_id, release_id)
            return PublishResult(app_id, release_id, online_url, operation, artifact.sha256)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # ---- 步骤 ----

    def _validate(self, artifact: WebArtifact) -> None:
        result = self._run(
            [sys.executable, str(VALIDATE_HTML), str(artifact.path), "--expect-sha256", artifact.sha256],
            capture_output=True, text=True, timeout=CLI_TIMEOUT_SEC,
        )
        if result.returncode != 0:
            raise MiaodaFallbackError(
                f"网页预检未通过（validate_html.py exit={result.returncode}），已停止发布：{_tail(result.stdout)}"
            )

    def _resolve_target(
        self, env: dict, artifact: WebArtifact, known_online_urls: Sequence[str]
    ) -> tuple[str, str]:
        apps = self._list_mine(env, keyword=artifact.app_name)
        known = {u for u in known_online_urls if u}
        for app in apps:
            if app.get("online_url") and app.get("online_url") in known:
                return str(app["app_id"]), "update"
        same_name = [a for a in apps if a.get("name") == artifact.app_name]
        if same_name:
            ids = ", ".join(str(a.get("app_id")) for a in same_name)
            raise MiaodaFallbackError(
                f"已存在同名妙搭应用「{artifact.app_name}」（{ids}），但无法确认是本任务的应用，"
                "按妙搭发布规则不自动覆盖，请人工确认后处理。"
            )
        created = self._cli_json(
            env, "+create", "--name", artifact.app_name, "--app-type", "html",
            "--description", f"Topic6 {artifact.app_name} 网页版报告",
            step="+create",
        )
        app_id = str(_dig(created, "app_id") or "")
        if not app_id:
            raise MiaodaFallbackError("+create 未返回 app_id")
        log.info("topic6 miaoda fallback created app=%s name=%s", app_id, artifact.app_name)
        return app_id, "create"

    def _list_mine(self, env: dict, keyword: str) -> list[dict]:
        items: list[dict] = []
        token = ""
        for _ in range(LIST_MAX_PAGES):
            args = ["+list", "--ownership", "mine", "--keyword", keyword, "--page-size", str(LIST_PAGE_SIZE)]
            if token:
                args += ["--page-token", token]
            data = self._cli_json(env, *args, step="+list")
            payload = data.get("data") if isinstance(data.get("data"), dict) else {}
            items += [i for i in payload.get("items") or [] if isinstance(i, dict)]
            token = str(payload.get("page_token") or "")
            if not payload.get("has_more") or not token:
                break
        return items

    def _commit(self, env: dict, repo: Path, artifact: WebArtifact) -> None:
        if not (repo / ".git").is_dir():
            raise MiaodaFallbackError("+init 后未生成本地妙搭仓库")
        shutil.copyfile(artifact.path, repo / "index.html")
        self._git(env, repo, "add", "--", "index.html")
        staged = self._run(["git", "diff", "--cached", "--quiet"], cwd=repo, env=env,
                           capture_output=True, text=True, timeout=CLI_TIMEOUT_SEC)
        if staged.returncode == 0:
            return  # 内容与远端一致：不产生空提交，直接发布当前分支
        self._git(env, repo, "-c", "user.name=topic6-gateway", "-c", "user.email=topic6-gateway@local",
                  "commit", "-q", "-m", "Publish predefined HTML (gateway fallback)")

    def _push(self, env: dict, repo: Path, app_id: str) -> None:
        try:
            self._git(env, repo, "push", "origin", f"HEAD:{RELEASE_BRANCH}")
        except MiaodaFallbackError:
            # skill 规定的鉴权恢复：+git-credential-init 后重试原 push
            self._cli_json(env, "+git-credential-init", "--app-id", app_id,
                           step="+git-credential-init", app_id=app_id, cwd=repo)
            self._git(env, repo, "push", "origin", f"HEAD:{RELEASE_BRANCH}")

    def _wait_release(self, env: dict, app_id: str, release_id: str) -> str:
        deadline = time.monotonic() + RELEASE_POLL_TIMEOUT_SEC
        status = ""
        data: dict = {}
        while True:
            data = self._cli_json(env, "+release-get", "--app-id", app_id, "--release-id", release_id,
                                  step="+release-get", app_id=app_id)
            status = str(_dig(data, "status") or "")
            if status in ("finished", "failed") or time.monotonic() >= deadline:
                break
            self._sleep(RELEASE_POLL_INTERVAL_SEC)
        if status != "finished":
            raise MiaodaFallbackError(
                f"妙搭发布未完成（status={status or '未知'}，app_id={app_id}，release_id={release_id}）"
            )
        url = str(_dig(data, "online_url") or "")
        if not url:
            url = str(_dig(self._cli_json(env, "+get", "--app-id", app_id, step="+get", app_id=app_id),
                           "online_url") or "")
        if not url:
            for app in self._list_mine(env, keyword=""):
                if str(app.get("app_id")) == app_id:
                    url = str(app.get("online_url") or "")
                    break
        if not url.startswith(("https://", "http://")):
            raise MiaodaFallbackError(f"发布已完成但未取到 online_url（app_id={app_id}，release_id={release_id}）")
        return url

    # ---- 子进程 ----

    def _env(self, home: Path, access_token: str) -> dict:
        path_parts = [str(Path(self._cli).parent)]
        if self._node_bin_dir:
            path_parts.append(self._node_bin_dir)
        path_parts += ["/usr/local/bin", "/usr/bin", "/bin"]
        env = {k: os.environ[k] for k in _PASSTHROUGH_ENV if os.environ.get(k)}
        env.update({
            "HOME": str(home),
            "PATH": ":".join(path_parts),
            "LARKSUITE_CLI_APP_ID": self._app_id,
            "LARKSUITE_CLI_USER_ACCESS_TOKEN": access_token,
            "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
            "GIT_TERMINAL_PROMPT": "0",
        })
        return env

    def _cli_json(self, env: dict, *args: str, step: str, app_id: str = "", cwd: Optional[Path] = None) -> dict:
        result = self._run(
            [self._cli, "apps", *args, "--as", "user", "--format", "json"],
            cwd=cwd, env=env, capture_output=True, text=True, timeout=CLI_TIMEOUT_SEC,
        )
        data = _parse_json(result.stdout) or _parse_json(result.stderr) or {}
        if result.returncode != 0 or data.get("ok") is not True:
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            detail = error.get("message") or _tail(result.stderr or result.stdout)
            where = f"（app_id={app_id}）" if app_id else ""
            raise MiaodaFallbackError(f"lark-cli apps {step} 失败{where}：{str(detail)[:300]}")
        return data

    def _git(self, env: dict, repo: Path, *args: str) -> None:
        result = self._run(["git", *args], cwd=repo, env=env, capture_output=True, text=True,
                           timeout=CLI_TIMEOUT_SEC)
        if result.returncode != 0:
            raise MiaodaFallbackError(f"git {args[0] if args[0] != '-c' else 'commit'} 失败：{_tail(result.stderr)}")


def _parse_json(text: str) -> Optional[dict]:
    text = (text or "").strip()
    start = text.find("{")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _dig(payload: object, key: str):
    """在 lark-cli 返回的 data 里递归找第一个 key（不同子命令嵌套层级不同）。"""
    if isinstance(payload, dict):
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
        for value in payload.values():
            found = _dig(value, key)
            if found not in (None, ""):
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _dig(value, key)
            if found not in (None, ""):
                return found
    return None


def _tail(text: str, limit: int = 300) -> str:
    text = re.sub(r"//[^/@\s]*@", "//***@", (text or "").strip())  # 去掉 URL 里的凭据
    return text[-limit:]


__all__ = [
    "MiaodaFallbackError",
    "MiaodaPublisher",
    "PublishResult",
    "WebArtifact",
    "locate_web_artifact",
]
