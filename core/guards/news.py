# core/guards/news.py
from core.guards import guard
from core.news_engine import check_news_blockade


@guard("news", scope="scan", order=10)
async def check(ctx):
    blocked, reason = await check_news_blockade()      # (False, "") if the feed is down or the blockade is off
    return reason if blocked else None