"""联网检索工具（需求 3.4 工具集：GoogleSearchTool / WikipediaTool 的免 Key 实现）。

数据源与降级链（需求 3.9：主工具失败 → 切备用数据源）：
    1) DuckDuckGo（duckduckgo-search，免 Key）—— 需求指定的主搜索源
    2) Wikipedia（wikipedia，免 Key）—— 需求指定的百科源
    3) Bing 网页解析（cn.bing.com，免 Key）—— 国内网络下 DDG/Wikipedia 不可达时的兜底

⚠️ 本机网络实测（无代理）：DuckDuckGo 与 Wikipedia 均 **ConnectTimeout**（被墙），
   因此 search_web() 会自动降级到 Bing；三者任一可用即可产出真实资料。
   被墙/失败的后端会进入冷却（默认 300s），避免每次调用都白等超时，保护需求 N2 的 8 分钟预算。
   挂上代理后 DDG/Wikipedia 会自动恢复为主源（无需改代码）。
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable, TypeVar
from urllib.parse import quote

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from app.config import settings
from app.core import metrics as m
from app.core.logging import logger

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HTTP_TIMEOUT = 8.0          # 单次 HTTP 超时（秒）：宁快失败也不拖垮任务
_CALL_TIMEOUT = 10.0         # 单个后端的硬性时限：第三方库内部会重试多个域名，必须兜住
_PREFLIGHT_TIMEOUT = 3.0     # 可达性预检超时
_COOLDOWN_SECONDS = 300.0    # 后端失败后的冷却时长，期间直接跳过
_T = TypeVar("_T")

# 后端冷却表：{"duckduckgo": 允许再次调用的时间戳}
_cooldown_until: dict[str, float] = {}
# 后端可达性（进程内缓存）：被墙的后端只付一次预检代价，后续任务零成本跳过
_reachability: dict[str, bool] = {}


def _in_cooldown(backend: str) -> bool:
    return time.time() < _cooldown_until.get(backend, 0.0)


def _start_cooldown(backend: str, reason: str) -> None:
    if not _in_cooldown(backend):
        logger.warning(
            "search backend unavailable, cooling down {}s | backend={} reason={}",
            int(_COOLDOWN_SECONDS), backend, reason[:120],
        )
    _cooldown_until[backend] = time.time() + _COOLDOWN_SECONDS


def reset_cooldowns() -> None:
    """清空冷却表与可达性缓存（测试/排障用）。"""
    _cooldown_until.clear()
    _reachability.clear()


def _preflight(backend: str, url: str) -> bool:
    """可达性预检（结果进程内缓存）。

    被墙的站点直接连接会长时间挂起（实测 Wikipedia 42s、DuckDuckGo 库 40s），
    故先用 3s 短探测判定，避免每个任务都白等一次连接超时（保护需求 N2 的 8 分钟预算）。
    """
    cached = _reachability.get(backend)
    if cached is not None:
        return cached

    import httpx

    try:
        httpx.get(url, timeout=_PREFLIGHT_TIMEOUT, follow_redirects=True)
        ok = True
    except Exception as exc:
        ok = False
        _start_cooldown(backend, f"preflight failed: {type(exc).__name__}")
    _reachability[backend] = ok
    return ok


def _with_timeout(fn: Callable[..., _T], seconds: float, *args: Any) -> _T:
    """给第三方库调用加硬性时限（超时即放弃该线程，不等它结束）。"""
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        return executor.submit(fn, *args).result(timeout=seconds)
    except TimeoutError as exc:  # concurrent.futures.TimeoutError 即内置 TimeoutError
        raise TimeoutError(f"调用超过 {seconds}s 硬性时限") from exc
    finally:
        executor.shutdown(wait=False)  # 不等待：被放弃的线程不应阻塞任务收尾


# ---------------------------------------------------------------------------
# 各后端实现（统一返回 [{"title","url","snippet"}, ...]）
# ---------------------------------------------------------------------------
def _duckduckgo(query: str, max_results: int) -> list[dict[str, Any]]:
    if not _preflight("duckduckgo", "https://duckduckgo.com/"):
        raise RuntimeError("可达性预检失败（站点被墙或不可达）")

    def _call() -> list[dict[str, Any]]:
        try:  # duckduckgo-search 已更名为 ddgs，两者都支持（优先维护中的新包名）
            from ddgs import DDGS  # type: ignore
        except ImportError:
            from duckduckgo_search import DDGS  # type: ignore

        with DDGS(timeout=int(_HTTP_TIMEOUT)) as ddgs:
            rows = list(ddgs.text(query, max_results=max_results))
        return [
            {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
            for r in rows
        ]

    # 硬性时限：ddgs 内部会依次尝试多个后端域名，实测单次调用远超其 timeout 参数
    return _with_timeout(_call, _CALL_TIMEOUT)


def _bing(query: str, max_results: int) -> list[dict[str, Any]]:
    import httpx
    from bs4 import BeautifulSoup

    resp = httpx.get(
        f"https://cn.bing.com/search?q={quote(query)}&setlang=en",
        headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
        timeout=_HTTP_TIMEOUT * 2,
        follow_redirects=True,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    out: list[dict[str, Any]] = []
    for li in soup.select("li.b_algo"):
        link = li.find("a", href=True)
        if not link:
            continue
        caption = li.select_one("div.b_caption p") or li.select_one("p")
        out.append(
            {
                "title": link.get_text(" ", strip=True),
                "url": link["href"],
                "snippet": caption.get_text(" ", strip=True) if caption else "",
            }
        )
        if len(out) >= max_results:
            break
    return out


_BACKENDS = {"duckduckgo": _duckduckgo, "bing": _bing}
_FALLBACK = {"duckduckgo": "bing", "bing": "duckduckgo"}


def search_web(query: str, max_results: int = 5, prefer: str = "duckduckgo") -> dict[str, Any]:
    """按降级链检索，返回 {"backend", "results", "errors"}。"""
    errors: dict[str, str] = {}
    order = [prefer] + [b for b in ("duckduckgo", "bing") if b != prefer]

    for idx, backend in enumerate(order):
        if _in_cooldown(backend):
            errors[backend] = "cooldown"
            continue
        started = time.perf_counter()
        try:
            results = _BACKENDS[backend](query, max_results)
        except Exception as exc:
            elapsed = time.perf_counter() - started
            errors[backend] = f"{type(exc).__name__}: {exc}"
            m.tool_calls_total.labels(tool=f"search:{backend}", status="failed").inc()
            m.tool_duration_seconds.labels(tool=f"search:{backend}").observe(elapsed)
            _start_cooldown(backend, errors[backend])
            continue

        elapsed = time.perf_counter() - started
        m.tool_calls_total.labels(tool=f"search:{backend}", status="success").inc()
        m.tool_duration_seconds.labels(tool=f"search:{backend}").observe(elapsed)
        if idx > 0:  # 走了降级链
            m.tool_fallbacks_total.labels(tool=prefer, fallback_tool=backend).inc()
            logger.info("search fallback | {} -> {} | query={}", prefer, backend, query[:60])
        return {"backend": backend, "results": results, "errors": errors}

    m.tool_calls_total.labels(tool=f"search:{prefer}", status="failed").inc()
    return {"backend": None, "results": [], "errors": errors}


def wikipedia_summary(query: str, sentences: int = 6, lang: str = "en") -> str | None:
    """Wikipedia 摘要（免 Key）；不可达/无条目时返回 None。"""
    if _in_cooldown("wikipedia"):
        return None
    if not _preflight("wikipedia", "https://en.wikipedia.org/wiki/Main_Page"):
        return None

    started = time.perf_counter()

    def _call() -> str:
        import wikipedia  # 延迟导入：未安装时不影响其它工具

        wikipedia.set_lang(lang)
        return wikipedia.summary(query, sentences=sentences, auto_suggest=True)

    try:
        # wikipedia 库默认不设超时，被墙时会挂起数十秒，必须加硬性时限
        summary = _with_timeout(_call, _CALL_TIMEOUT)
    except Exception as exc:
        elapsed = time.perf_counter() - started
        m.tool_calls_total.labels(tool="search:wikipedia", status="failed").inc()
        m.tool_duration_seconds.labels(tool="search:wikipedia").observe(elapsed)
        _start_cooldown("wikipedia", f"{type(exc).__name__}: {exc}")
        return None

    elapsed = time.perf_counter() - started
    m.tool_calls_total.labels(tool="search:wikipedia", status="success").inc()
    m.tool_duration_seconds.labels(tool="search:wikipedia").observe(elapsed)
    return summary


def _format_results(title: str, results: list[dict[str, Any]]) -> str:
    lines = [f"[{title}]"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']} — {r['snippet']} ({r['url']})")
    return "\n".join(lines)


def gather_company_context(
    company_name: str, ticker: str | None = None, max_results: int = 5
) -> tuple[str, list[str]]:
    """预取公司资料（不依赖 LLM 是否调用工具），返回 (资料文本, 数据源列表)。

    三步：Wikipedia 简介 → 公司与行业检索 → 近期新闻检索。
    """
    blocks: list[str] = []
    sources: list[str] = []

    summary = wikipedia_summary(company_name)
    if summary:
        blocks.append(f"[Wikipedia 摘要]\n{summary}")
        sources.append("wikipedia")

    profile_hit = search_web(
        f"{company_name} company profile industry main products competitors",
        max_results=max_results,
    )
    if profile_hit["results"]:
        blocks.append(_format_results("公司与行业检索", profile_hit["results"]))
        sources.append(profile_hit["backend"] or "")

    year = datetime.now().year
    news_hit = search_web(f"{company_name} news {year}", max_results=max_results)
    if news_hit["results"]:
        blocks.append(_format_results("近期新闻检索", news_hit["results"]))
        sources.append(news_hit["backend"] or "")

    # 去重且保持顺序
    seen: list[str] = []
    for s in sources:
        if s and s not in seen:
            seen.append(s)

    if not blocks:
        logger.warning("no context gathered | company={} errors={}", company_name, profile_hit["errors"])
    return "\n\n".join(blocks), seen


# ---------------------------------------------------------------------------
# CrewAI 工具封装（需求 3.5：name + description + args_schema）
# ---------------------------------------------------------------------------
class _SearchInput(BaseModel):
    query: str = Field(..., description="检索关键词，建议用英文以提高命中率")
    max_results: int = Field(5, ge=1, le=10, description="返回结果条数上限")


class DuckDuckGoSearchTool(BaseTool):
    name: str = "duckduckgo_search"
    description: str = (
        "用 DuckDuckGo 搜索网页，获取公司动态、行业新闻与竞争对手信息（免 API Key）。"
        "不可用时自动降级到 Bing。输入检索关键词，返回 JSON 格式的结果列表。"
    )
    args_schema: type[BaseModel] = _SearchInput

    def _run(self, query: str, max_results: int = 5) -> str:
        payload = search_web(query, max_results=max_results, prefer="duckduckgo")
        return json.dumps(payload, ensure_ascii=False)


class BingSearchTool(BaseTool):
    name: str = "bing_search"
    description: str = (
        "用 Bing 搜索网页（免 API Key），作为 DuckDuckGo 不可达时的备用搜索源。"
        "输入检索关键词，返回 JSON 格式的结果列表。"
    )
    args_schema: type[BaseModel] = _SearchInput

    def _run(self, query: str, max_results: int = 5) -> str:
        payload = search_web(query, max_results=max_results, prefer="bing")
        return json.dumps(payload, ensure_ascii=False)


class _WikiInput(BaseModel):
    query: str = Field(..., description="要查询的条目名，如 Apple Inc.")
    sentences: int = Field(6, ge=1, le=15, description="摘要句数")


class WikipediaSearchTool(BaseTool):
    name: str = "wikipedia_search"
    description: str = (
        "查询 Wikipedia 获取公司基础信息（成立时间、业务、创始人等，免 API Key）。"
        "输入条目名，返回摘要文本；不可达时返回提示字符串。"
    )
    args_schema: type[BaseModel] = _WikiInput

    def _run(self, query: str, sentences: int = 6) -> str:
        summary = wikipedia_summary(query, sentences=sentences)
        if summary:
            return summary
        return f"Wikipedia 不可用或未找到条目：{query}（可改用 duckduckgo_search 或 bing_search）"
