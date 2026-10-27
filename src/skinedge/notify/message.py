"""Текст уведомлений о возможностях (HTML для Telegram)."""

from __future__ import annotations

from decimal import Decimal
from html import escape

from skinedge.games import title
from skinedge.paper import Report
from skinedge.scanner import Opportunity


def _pct(value: Decimal | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def format_opportunity(opp: Opportunity) -> str:
    """Один блок уведомления."""
    r = opp.result
    status = "полный расчёт" if r.complete else "⚠️ неполный расчёт"
    lines = [
        f"<b>{escape(opp.name)}</b> · {title(opp.buy_quote.app_id)}",
        f"{r.buy_market} → {r.sell_market} · {status}",
        f"Купить: {r.buy_price} · продать ~{r.sell_price}",
        f"<i>{escape(opp.sell.basis)}</i>",
        f"Прибыль <b>{r.profit}</b> · ROI <b>{_pct(r.roi)}</b> · запас {_pct(r.safety_margin)}",
        f"Стоимость {r.total_cost} · выручка {r.net_proceeds} · заморозка {r.lock_days} дн.",
    ]
    lines += [f"• {escape(flag)}" for flag in opp.flags]
    links = []
    if opp.buy_url:
        links.append(f'<a href="{escape(opp.buy_url, quote=True)}">купить: {r.buy_market}</a>')
    if opp.sell_url:
        links.append(f'<a href="{escape(opp.sell_url, quote=True)}">продать: {r.sell_market}</a>')
    if links:
        lines.append(" · ".join(links))
    return "\n".join(lines)


def format_digest(opportunities: list[Opportunity], total: int) -> str:
    """Сводка новых возможностей одного скана.

    Args:
        opportunities: Возможности для уведомления, лучшие первыми.
        total: Сколько всего возможностей нашёл скан (включая уже отправленные).
    """
    head = f"🔎 SkinEdge: новых возможностей {len(opportunities)} (всего в скане {total})"
    blocks = [head, *(format_opportunity(o) for o in opportunities)]
    blocks.append(
        "Это оценка по текущим ценам, а не гарантия. SkinEdge ничего не покупает и не продаёт: "
        "перед сделкой проверь лот, float и наклейки вручную."
    )
    return "\n\n".join(blocks)


def format_paper_report(report: Report, days: int) -> str:
    """Ежедневный отчёт по виртуальным сделкам."""
    lines = [f"📒 <b>SkinEdge: виртуальные сделки за {days} дн.</b>"]
    if not report.trades:
        lines.append("Сигналов пока нет: ни одна возможность не прошла стандартные пороги.")
        return "\n".join(lines)
    complete = sum(t.complete for t in report.trades)
    lines.append(
        f"Сигналов: {len(report.trades)} (полных расчётов {complete}), "
        f"средний ROI в момент сигнала {_pct(report.avg_signal_roi)}"
    )
    for s in report.summaries:
        if s.evaluated == 0:
            lines.append(f"{s.label}: данных пока нет")
            continue
        lines.append(
            f"{s.label}: проверено {s.evaluated}, в плюсе {s.profitable}, "
            f"не продать уверенно {s.unsellable}, средний ROI {_pct(s.avg_roi)}, "
            f"прибыль {s.total_profit} EUR"
        )
    lines.append(
        "<i>Главная точка: 7д, когда купленный предмет можно передать. "
        "Это пересчёт по рыночным ценам, сделки не совершались.</i>"
    )
    return "\n".join(lines)
