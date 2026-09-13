"""
daily_top_prompts.py
---------------------
每天从 Reddit + GitHub 抓取「prompt 相关」的热门内容，尝试直接提取出
「可以拿来就用的 prompt 正文」（而不是只给一个链接让你自己点进去看），
去重、按各来源内部百分位排序后，取 Top 10 推送到你自己的 Notion 数据库里。

数据源：
1. Reddit 官方公开只读 JSON 接口（合规、免费、无需 API Key）
   - r/PromptEngineering
   - r/ChatGPTPromptGenius
   - r/aipromptprogramming
   - r/ClaudeAI
   - r/LocalLLaMA
   -> 帖子正文（selftext）本身往往就是可用的 prompt 文本，直接抓全文。
2. GitHub 官方 API（合规、免费）
   - 先用 Search API 搜出「近期活跃的 prompt 相关仓库」
   - 再对每个仓库额外请求一次 README，用正则提取里面的代码块
   （大多数 prompt 仓库会把实际提示词内容放在 ```代码块``` 里），
   抽取最像"可用 prompt"的一段文本，而不是只给仓库描述。

推送目标：
- 你自己的 Notion 数据库（通过 Notion 官方 API）

用法：
    pip install requests
    设置环境变量后运行：
    export GITHUB_TOKEN=你的token   (Windows: set GITHUB_TOKEN=你的token)
    export NOTION_TOKEN=你的token
    python daily_top_prompts.py

安全说明：
- GITHUB_TOKEN 和 NOTION_TOKEN 不再写死在代码里，而是从环境变量读取。
  这样即使把这份代码公开发布到 GitHub 仓库，也不会泄露你的真实密钥。
- 如果你之前用过硬编码在代码里的旧 token，请务必去 GitHub / Notion
  后台吊销并重新生成，因为旧 token 已经暴露过。

建议：用 cron / Windows 计划任务，每天定时跑一次即可自动同步到 Notion。

【GitHub 配置说明】
- 这一版新增了 README 抓取，请求量比只搜索多，配置了 GITHUB_TOKEN 之后
  限速会从每小时 60 次提升到 5000 次，基本不用担心跑不完。

【Notion 配置步骤】（只需要做一次）：
1. Notion 数据库需要包含这几列（列名和类型要对上）：
   - Name （类型: Title）
   - Source （类型: Text）
   - URL （类型: URL）
   - Score （类型: Number）
   - Comments （类型: Number）
   - Preview （类型: Text —— 这一版存放的是"提取出来的可用 prompt 正文"，
     不再是简单的一句话描述）
2. 把数据库分享（Connections）给你创建的 Connection。
"""

import os
import re
import time
import requests
from datetime import datetime, timedelta, timezone

# ---------- 配置区 ----------

SUBREDDITS = [
    "PromptEngineering",
    "ChatGPTPromptGenius",
    "aipromptprogramming",
    "ClaudeAI",
    "LocalLLaMA",
]

# Reddit 官方公开接口要求必须带自定义 User-Agent，否则会被限流/拒绝
REDDIT_HEADERS = {
    "User-Agent": "daily-prompt-aggregator/0.2 (by u/Papillon_0091)"
}

# GitHub 搜索关键词，可以自行增删。语法参考 GitHub 官方文档：
# https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories
GITHUB_QUERIES = [
    "prompt engineering in:name,description",
    "chatgpt prompts in:name,description",
    "topic:prompts",
    "topic:llm-prompts",
]

GITHUB_RECENT_DAYS = 30  # 只看最近 N 天内有更新的仓库，近似"新鲜"内容
GITHUB_PER_QUERY = 10    # 每个查询取多少条

# 从环境变量读取
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

TOP_N = 10

# Notion 单个 rich_text 字段最多 2000 字符，这里统一按这个上限截断
MAX_PROMPT_CHARS = 1900

# ---- Notion 配置（从环境变量读取） ----
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
NOTION_VERSION = "2022-06-28"  # Notion API 版本号，官方文档要求带这个 header

# ---------- 数据源：Reddit ----------

def fetch_reddit_top(subreddit: str, timeframe: str = "day", limit: int = 25):
    """
    抓取某个 subreddit 当天热度最高的帖子，直接把帖子正文（selftext）
    当作候选的"可用 prompt 内容"，因为这几个 subreddit 里的帖子
    大多数就是直接贴提示词原文。
    使用 Reddit 官方公开 JSON 接口：https://www.reddit.com/r/<subreddit>/top.json
    不需要登录 / API Key，但请遵守 Reddit 的速率限制（每个请求间隔 >=1s 比较稳妥）。
    """
    url = f"https://www.reddit.com/r/{subreddit}/top.json"
    params = {"t": timeframe, "limit": limit}
    resp = requests.get(url, headers=REDDIT_HEADERS, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    items = []
    for child in data.get("data", {}).get("children", []):
        post = child["data"]
        selftext = (post.get("selftext") or "").strip()
        # 有些帖子是纯链接贴，没有正文，这种就退回用标题当内容
        prompt_text = selftext if selftext else post.get("title", "")
        items.append({
            "source": f"reddit/r/{subreddit}",
            "title": post.get("title", "").strip(),
            "url": "https://reddit.com" + post.get("permalink", ""),
            "score": post.get("score", 0),  # 净赞同数，作为热度指标
            "num_comments": post.get("num_comments", 0),
            "created_utc": post.get("created_utc", 0),
            "prompt_text": prompt_text[:MAX_PROMPT_CHARS],
        })
    return items

# ---------- 数据源：GitHub ----------

def _github_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "daily-prompt-aggregator/0.2",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers

def extract_prompt_from_readme(full_name: str) -> str:
    """
    抓取仓库 README 原始内容，从里面尝试提取"看起来像可用 prompt"的一段文字。

    策略（尽量简单、可解释）：
    1. 用 GitHub Contents API 拿 README 的原始文本
       （Accept: application/vnd.github.raw+json 会直接返回纯文本，不用再解 base64）
    2. 用正则找出所有 ```代码块```，这是大多数 prompt 仓库存放实际提示词的地方
       （比如 system prompt、模板、示例问答）
    3. 过滤掉太短（可能只是命令行示例）或太长（超过 Notion 字段上限）的代码块，
       在剩下的里面选最长的 1-2 个拼在一起，作为"提取出的 prompt 正文"
    4. 如果整个 README 都没有代码块（比如它本身就是纯文字合集），
       退而求其次：去掉图片/徽章/标题符号后，取正文的前一段文字作为预览

    注意：对于像 "awesome-chatgpt-prompts" 这种成百上千条 prompt 的大合集仓库，
    这里提取到的只是 README 里排在最前面、篇幅较长的那一两段，
    不代表整个仓库里"最好用"的那一条——这类大合集本质上还是需要你自己点进去挑。
    """
    url = f"https://api.github.com/repos/{full_name}/readme"
    headers = _github_headers()
    headers["Accept"] = "application/vnd.github.raw+json"

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return ""
        readme_text = resp.text
    except Exception:
        return ""

    # 找所有 ```xxx``` 代码块，忽略语言标注那一行
    code_blocks = re.findall(r"```(?:[a-zA-Z0-9_-]*\n)?(.*?)```", readme_text, flags=re.DOTALL)
    # 过滤掉过短（可能是 pip install 之类的命令）或空白块
    candidates = [c.strip() for c in code_blocks if 40 <= len(c.strip()) <= 3000]

    if candidates:
        candidates.sort(key=len, reverse=True)
        combined = "\n\n---\n\n".join(candidates[:2])
        return combined[:MAX_PROMPT_CHARS]

    # 没有代码块，退化成取正文前几段有效文字
    lines = readme_text.splitlines()
    text_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("#", "!", "[![", "<")):  # 跳过标题/徽章/HTML
            continue
        text_lines.append(stripped)
        if sum(len(l) for l in text_lines) > 600:
            break
    return " ".join(text_lines)[:MAX_PROMPT_CHARS]

def fetch_github_top(query: str, per_page: int = GITHUB_PER_QUERY):
    """
    用 GitHub 官方 Search API 搜索 prompt 相关仓库，按 star 数排序，
    并限定为最近 GITHUB_RECENT_DAYS 天内有更新的仓库，近似代表"最近比较活跃/在被关注"的项目，
    再对每个仓库额外抓一次 README 提取正文内容。
    文档：https://docs.github.com/en/rest/search/search#search-repositories
    """
    since_date = (datetime.now(timezone.utc) - timedelta(days=GITHUB_RECENT_DAYS)).strftime("%Y-%m-%d")
    full_query = f"{query} pushed:>{since_date}"

    url = "https://api.github.com/search/repositories"
    params = {
        "q": full_query,
        "sort": "stars",
        "order": "desc",
        "per_page": per_page,
    }
    resp = requests.get(url, headers=_github_headers(), params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    items = []
    for repo in data.get("items", []):
        full_name = repo.get("full_name", "")
        description = (repo.get("description") or "").strip()

        prompt_text = extract_prompt_from_readme(full_name)
        if not prompt_text:
            # README 抓取失败时，至少还有仓库描述可以看
            prompt_text = description
        time.sleep(1)  # 每抓一个 README 都稍微限速一下，避免连续触发限速

        items.append({
            "source": "github",
            "title": full_name,
            "url": repo.get("html_url", ""),
            "score": repo.get("stargazers_count", 0),  # star 数作为热度指标
            "num_comments": repo.get("open_issues_count", 0),
            "created_utc": repo.get("pushed_at", ""),
            "prompt_text": prompt_text,
        })
    return items

# ---------- 排序与去重 ----------

def rank_top_prompts(all_items, top_n=TOP_N):
    """
    公平排序策略：「各来源内部先排名，再按百分位归一化」。

    背景：Reddit 的净赞同数和 GitHub 的 star 数量级本身不是一个体系，
    如果直接混在一起按原始 score 排序，量级大的来源会永远霸榜，
    量级小但在自己领域里其实很优质的内容反而永远上不了榜。

    做法：
    1. 按 item["source"] 分组（每个 subreddit、"github" 各算一组）。
    2. 组内按原始 score 从高到低排序，把排名换算成百分位：
       组内排第 1 名（最高分）percentile 接近 1.0，
       排最后一名 percentile 接近 1/组内数量。
    3. 所有来源的百分位放在同一把尺子上，直接比较、统一排序，
       这样每个来源都有公平的机会进入最终 Top N。
    """
    # 先按标题去重（同一条内容可能被多个查询重复抓到）
    seen_titles = set()
    deduped = []
    for item in all_items:
        key = item["title"][:40].lower()
        if key in seen_titles:
            continue
        seen_titles.add(key)
        deduped.append(item)

    # 按来源分组
    groups = {}
    for item in deduped:
        groups.setdefault(item["source"], []).append(item)

    # 组内排序 + 计算百分位
    for source, group_items in groups.items():
        group_items.sort(key=lambda x: x["score"], reverse=True)
        n = len(group_items)
        for idx, item in enumerate(group_items):
            item["percentile"] = round((n - idx) / n, 4)

    # 按百分位统一排序，百分位相同时用原始 score 做次要排序（打破并列）
    ranked = sorted(
        deduped,
        key=lambda x: (x["percentile"], x["score"]),
        reverse=True,
    )
    return ranked[:top_n]

# ---------- 推送到 Notion ----------

def push_to_notion(item: dict):
    """
    往 Notion 数据库里新建一条 page。Preview 字段这一版存放的是
    "提取出来的可用 prompt 正文"，而不是一句话描述——目的是让你在
    Notion 表格里直接就能看到、复制可用的内容，不用再点进 URL。
    要求数据库里已经有 Name(Title) / Source(Text) / URL(URL) /
    Score(Number) / Comments(Number) / Preview(Text) 这几列。
    """
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    payload = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "Name": {
                "title": [{"text": {"content": item["title"][:200]}}]
            },
            "Source": {
                "rich_text": [{"text": {"content": item["source"]}}]
            },
            "URL": {
                "url": item["url"]
            },
            "Score": {
                "number": item["score"]
            },
            "Comments": {
                "number": item["num_comments"]
            },
            "Preview": {
                "rich_text": [{"text": {"content": item["prompt_text"][:2000]}}]
            },
        },
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    if resp.status_code >= 300:
        print(f"[警告] 推送到 Notion 失败: {resp.status_code} {resp.text}")
    else:
        print(f"[已推送] {item['title'][:50]}")

# ---------- 主流程 ----------

def main():
    all_items = []

    # 1. Reddit
    for sub in SUBREDDITS:
        try:
            items = fetch_reddit_top(sub)
            all_items.extend(items)
        except Exception as e:
            print(f"[警告] 抓取 r/{sub} 失败: {e}")
        time.sleep(1)  # 礼貌性限速，避免被 Reddit 拉黑

    # 2. GitHub
    for query in GITHUB_QUERIES:
        try:
            items = fetch_github_top(query)
            all_items.extend(items)
        except Exception as e:
            print(f"[警告] GitHub 查询 '{query}' 失败: {e}")
        time.sleep(2)  # 稍微多留点间隔

    # 3. 排序 + 推送
    top_items = rank_top_prompts(all_items, TOP_N)

    print(f"共抓取 {len(all_items)} 条，去重排序后取前 {len(top_items)} 条，开始推送到 Notion：\n")

    for item in top_items:
        pct = item.get("percentile", 0)
        print(f"[百分位 {pct:.0%}] [{item['source']}] {item['title'][:50]}")
        push_to_notion(item)
        time.sleep(0.3)  # Notion API 也有速率限制，稍微留点间隔

    print(f"\n完成，当日生成时间: {datetime.now(timezone.utc).isoformat()}")

if __name__ == "__main__":
    main()
