"""Busca os dados de Meta Ads da conta Lopes Diálogo no Windsor.ai e grava em metrics.json.

Roda no GitHub Actions (ver .github/workflows/atualizar-dados.yml).
A chave da API vem do secret WINDSOR_API_KEY — nunca coloque a chave no código.

Formato de saída (lido pelo index.html):
{
  "updatedAt": "...",
  "campaigns": {
    "<nome da campanha>": {
      "campaign": "...",
      "daily": [{"d","spend","impr","clicks","leads","reach"}, ...],   # desde 01/01/2026
      "breakdown": {"last7": {...}, "month": {...}}                     # leads por gênero/idade/plataforma/dispositivo
    }
  }
}
O index.html liga cada produto (PRODUCTS) à sua campanha pelo nome exato.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

ACCOUNT_ID = "129584587555584"  # Diálogo Engenharia e Construção (Meta Ads)
# Lead = "Leads (formulário)" do Gerenciador de Anúncios (formulário instantâneo/Messenger
# dentro do Facebook/Instagram). NÃO inclui leads do pixel do site, para bater com o Meta.
LEAD_FIELD = "actions_onsite_conversion_lead_grouped"
START_DATE = "2026-01-01"       # início do acompanhamento de verba
TZ = timezone(timedelta(hours=-3))  # America/Sao_Paulo
OUT = os.path.join(os.path.dirname(__file__), "..", "metrics.json")


class WindsorError(Exception):
    pass


def windsor(api_key, fields, date_from, date_to):
    params = urllib.parse.urlencode({
        "api_key": api_key,
        "date_from": date_from,
        "date_to": date_to,
        "fields": ",".join(fields),
        "select_accounts": ACCOUNT_ID,
    })
    url = f"https://connectors.windsor.ai/facebook?{params}"
    try:
        with urllib.request.urlopen(url, timeout=300) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:800]
        raise WindsorError(f"HTTP {e.code}: {body}")
    except Exception as e:  # rede, timeout, JSON inválido
        raise WindsorError(f"{type(e).__name__}: {e}"[:800])
    rows = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise WindsorError(f"resposta inesperada: {str(payload)[:500]}")
    return rows


def n(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def main() -> int:
    api_key = (os.environ.get("WINDSOR_API_KEY") or "").strip()
    if not api_key:
        print("::error::WINDSOR_API_KEY não definido")
        return 1

    today = datetime.now(TZ).date()
    # "Últimos 7 dias" igual ao Meta: 7 dias completos, de ontem para trás (sem hoje)
    last7_from = (today - timedelta(days=7)).isoformat()
    last7_to = (today - timedelta(days=1)).isoformat()
    month_from = today.replace(day=1).isoformat()
    today_s = today.isoformat()

    try:
        daily_rows = windsor(api_key,
            ["date", "campaign", "spend", "impressions", "link_clicks", LEAD_FIELD, "reach"],
            START_DATE, today_s)
        bd = {}
        # Alcance não pode ser somado dia a dia (a mesma pessoa conta 1x no período),
        # então busca o alcance já consolidado pelo Meta para cada período.
        reach = {}
        periods = (("last7", last7_from, last7_to), ("month", month_from, today_s))
        for key, frm, to in periods:
            acc_rows = windsor(api_key, ["account_name", "reach"], frm, to)
            camp_rows = windsor(api_key, ["campaign", "reach"], frm, to)
            reach[key] = {
                "account": int(sum(n(r.get("reach")) for r in acc_rows)),
                "campaigns": {r["campaign"]: int(n(r.get("reach"))) for r in camp_rows if r.get("campaign")},
            }
        for key, frm, to in periods:
            bd[key] = (
                windsor(api_key, ["campaign", "gender", "age", LEAD_FIELD], frm, to),
                windsor(api_key, ["campaign", "publisher_platform", "device_platform", LEAD_FIELD], frm, to),
            )
    except WindsorError as e:
        print(f"::error::ERRO Windsor {e}")
        return 1

    if not daily_rows:
        # Não sobrescreve dados bons com um retorno vazio (falha temporária da API).
        print("Windsor retornou 0 linhas; mantendo metrics.json anterior")
        return 0

    # ---- série diária por campanha ----
    daily = defaultdict(lambda: defaultdict(lambda: {"spend": 0.0, "impr": 0, "clicks": 0, "leads": 0, "reach": 0}))
    for r in daily_rows:
        c, d = r.get("campaign"), r.get("date")
        if not c or not d:
            continue
        x = daily[c][d]
        x["spend"] += n(r.get("spend"))
        x["impr"] += int(n(r.get("impressions")))
        x["clicks"] += int(n(r.get("link_clicks")))
        x["leads"] += int(n(r.get(LEAD_FIELD)))
        x["reach"] += int(n(r.get("reach")))

    campaigns = {}
    for c, days in daily.items():
        campaigns[c] = {
            "campaign": c,
            "daily": [
                {"d": d, "spend": round(v["spend"], 2), "impr": v["impr"], "clicks": v["clicks"],
                 "leads": v["leads"], "reach": v["reach"]}
                for d, v in sorted(days.items())
            ],
            "breakdown": {},
        }

    # ---- leads por gênero / idade / plataforma / dispositivo ----
    for key, (ga_rows, pd_rows) in bd.items():
        acc = defaultdict(lambda: {"gender": defaultdict(int), "age": defaultdict(int),
                                   "platform": defaultdict(int), "device": defaultdict(int)})
        for r in ga_rows:
            c = r.get("campaign")
            if not c:
                continue
            leads = int(n(r.get(LEAD_FIELD)))
            acc[c]["gender"][r.get("gender") or "unknown"] += leads
            acc[c]["age"][r.get("age") or "unknown"] += leads
        for r in pd_rows:
            c = r.get("campaign")
            if not c:
                continue
            leads = int(n(r.get(LEAD_FIELD)))
            acc[c]["platform"][r.get("publisher_platform") or "unknown"] += leads
            acc[c]["device"][r.get("device_platform") or "unknown"] += leads
        for c, dims in acc.items():
            if c in campaigns:
                campaigns[c]["breakdown"][key] = {dim: dict(v) for dim, v in dims.items()}

    out = {
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "account": "Lopes Diálogo",
        "campaigns": campaigns,
        "reach": reach,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(daily_rows)} linhas diárias, {len(campaigns)} campanhas gravadas em metrics.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
