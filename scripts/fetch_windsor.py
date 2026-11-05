"""Busca os dados de Meta Ads da conta Lopes Diálogo no Windsor.ai e grava em metrics.json.

Roda no GitHub Actions (ver .github/workflows/atualizar-dados.yml), de hora em hora.
A chave da API vem do secret WINDSOR_API_KEY — nunca coloque a chave no código.

Formato de saída (lido pelo index.html):
{
  "updatedAt": "2026-11-04T14:07:00Z",      # UTC
  "today": "2026-11-04",                     # data de hoje em São Paulo
  "account": {"name", "spendCap", "amountSpent", "balance"},   # em reais
  "daily": {"<campanha>": {"AAAA-MM-DD": [spend, impr, clicks, leads], ...}},
  "reach": {"last7"|"prev7"|"month": {"account": n, "campaigns": {"<campanha>": n}}},
  "breakdown": {"last7"|"month": {"gender": {...}, "age": {...}}},      # leads, conta inteira
  "engagement": {"last7"|"month": {"spend", "impr", "reach", "videoViews", "reactions"}},
  "budgets": {"<campanha>": {"type": "CBO"|"ABO", "daily": reais, "adsets": n}},
  "topAds": [{"campaign", "name", "leads", "spend", "link", "thumb"}],  # últimos 7 dias, conta inteira
  "adsByCampaign": {"last7"|"month": {"<campanha>": [mesmo formato de topAds]}}  # melhores de cada campanha
}
"""
import glob
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

ACCOUNT_ID = "129584587555584"  # Diálogo Engenharia e Construção (Meta Ads)
# Lead = "Leads (formulário)" do Gerenciador de Anúncios. NÃO usar actions_lead
# (soma os leads do pixel do site e não bate com o Gerenciador).
LEAD_FIELD = "actions_onsite_conversion_lead_grouped"
ENGAGEMENT_CAMPAIGN = "[LUZIAC] Engajamento"
START_DATE = "2026-01-01"       # primeiro dia da série diária (período personalizado)
TOP_ADS = 6
TOP_ADS_PER_CAMPAIGN = 6   # o painel junta as campanhas de cada empreendimento
TZ = timezone(timedelta(hours=-3))  # America/Sao_Paulo
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
OUT = os.path.join(ROOT, "metrics.json")
ADS_DIR = os.path.join(ROOT, "assets", "ads")


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


def save_thumb(url, key):
    """Baixa a miniatura do anúncio para assets/ads (o link do Meta expira em poucos dias)."""
    if not url:
        return None
    name = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    existing = glob.glob(os.path.join(ADS_DIR, name + ".*"))
    if existing:
        return "assets/ads/" + os.path.basename(existing[0])
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (painel)"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            data = resp.read()
    except Exception as e:
        print(f"::warning::miniatura não baixada ({key}): {type(e).__name__}")
        return None
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/avif": "avif",
           "image/gif": "gif"}.get(ctype)
    if not ext:
        path = urllib.parse.urlparse(url).path.lower()
        ext = next((e for e in ("jpg", "jpeg", "png", "webp", "avif", "gif") if path.endswith("." + e)), "jpg")
    os.makedirs(ADS_DIR, exist_ok=True)
    fn = f"{name}.{ext}"
    with open(os.path.join(ADS_DIR, fn), "wb") as f:
        f.write(data)
    return "assets/ads/" + fn


def main() -> int:
    api_key = (os.environ.get("WINDSOR_API_KEY") or "").strip()
    if not api_key:
        print("::error::WINDSOR_API_KEY não definido")
        return 1

    today = datetime.now(TZ).date()
    yesterday = today - timedelta(days=1)
    # "Últimos 7 dias" igual ao Meta: 7 dias completos, de ontem para trás (sem hoje)
    last7 = ((today - timedelta(days=7)).isoformat(), yesterday.isoformat())
    prev7 = ((today - timedelta(days=14)).isoformat(), (today - timedelta(days=8)).isoformat())
    # "Este mês": do dia 1 até ontem (no dia 1, mostra o mês anterior inteiro)
    month = (yesterday.replace(day=1).isoformat(), yesterday.isoformat())
    today_s = today.isoformat()
    recent_from = (today - timedelta(days=2)).isoformat()

    try:
        daily_rows = windsor(api_key,
            ["date", "campaign", "spend", "impressions", "link_clicks", LEAD_FIELD],
            START_DATE, today_s)

        # Alcance não pode ser somado dia a dia: busca o consolidado do Meta por período.
        reach = {}
        for key, (frm, to) in (("last7", last7), ("prev7", prev7), ("month", month)):
            acc_rows = windsor(api_key, ["account_name", "reach"], frm, to)
            camp_rows = windsor(api_key, ["campaign", "reach"], frm, to)
            reach[key] = {
                "account": int(sum(n(r.get("reach")) for r in acc_rows)),
                "campaigns": {r["campaign"]: int(n(r.get("reach"))) for r in camp_rows if r.get("campaign")},
            }

        breakdown = {}
        for key, (frm, to) in (("last7", last7), ("month", month)):
            rows = windsor(api_key, ["gender", "age", LEAD_FIELD], frm, to)
            g, a = defaultdict(int), defaultdict(int)
            for r in rows:
                leads = int(n(r.get(LEAD_FIELD)))
                g[r.get("gender") or "unknown"] += leads
                a[r.get("age") or "unknown"] += leads
            breakdown[key] = {"gender": dict(g), "age": dict(a)}

        engagement = {}
        for key, (frm, to) in (("last7", last7), ("month", month)):
            rows = windsor(api_key, ["campaign", "spend", "impressions", "actions_video_view",
                                     "actions_post_reaction"], frm, to)
            rows = [r for r in rows if r.get("campaign") == ENGAGEMENT_CAMPAIGN]
            engagement[key] = {
                "spend": round(sum(n(r.get("spend")) for r in rows), 2),
                "impr": int(sum(n(r.get("impressions")) for r in rows)),
                "videoViews": int(sum(n(r.get("actions_video_view")) for r in rows)),
                "reactions": int(sum(n(r.get("actions_post_reaction")) for r in rows)),
                "reach": reach[key]["campaigns"].get(ENGAGEMENT_CAMPAIGN, 0),
            }

        # Orçamento diário atual. Valores do Meta vêm em centavos.
        # CBO = orçamento da campanha; ABO = soma dos conjuntos ativos.
        bud_rows = windsor(api_key, ["campaign", "campaign_effective_status", "campaign_daily_budget",
                                     "adset_id", "adset_effective_status", "adset_daily_budget"],
                           recent_from, today_s)

        acc_rows = windsor(api_key, ["account_name", "spend_cap", "amount_spent", "account_balance"],
                           recent_from, today_s)

        ad_fields = ["campaign", "ad_id", "ad_name", "thumbnail_url",
                     "ad_preview_shareable_link", "instagram_permalink_url", "spend", LEAD_FIELD]
        ad_rows = {"last7": windsor(api_key, ad_fields, last7[0], last7[1]),
                   "month": windsor(api_key, ad_fields, month[0], month[1])}
    except WindsorError as e:
        print(f"::error::ERRO Windsor {e}")
        return 1

    if not daily_rows:
        # Não sobrescreve dados bons com um retorno vazio (falha temporária da API).
        print("Windsor retornou 0 linhas; mantendo metrics.json anterior")
        return 0

    # ---- série diária por campanha: [investimento, impressões, cliques no link, leads] ----
    daily = defaultdict(lambda: defaultdict(lambda: [0.0, 0, 0, 0]))
    for r in daily_rows:
        c, d = r.get("campaign"), r.get("date")
        if not c or not d:
            continue
        x = daily[c][d]
        x[0] += n(r.get("spend"))
        x[1] += int(n(r.get("impressions")))
        x[2] += int(n(r.get("link_clicks")))
        x[3] += int(n(r.get(LEAD_FIELD)))
    daily_out = {}
    for c, days in daily.items():
        kept = {d: [round(v[0], 2), v[1], v[2], v[3]] for d, v in sorted(days.items()) if any(v)}
        if kept:
            daily_out[c] = kept

    # ---- orçamentos ----
    camp_budget, adset_budget = {}, defaultdict(dict)
    for r in bud_rows:
        c = r.get("campaign")
        if not c or r.get("campaign_effective_status") != "ACTIVE":
            continue
        if r.get("campaign_daily_budget") not in (None, "", 0):
            camp_budget[c] = n(r.get("campaign_daily_budget")) / 100
        elif r.get("adset_effective_status") == "ACTIVE" and r.get("adset_daily_budget") not in (None, ""):
            adset_budget[c][r.get("adset_id")] = n(r.get("adset_daily_budget")) / 100
    budgets = {c: {"type": "CBO", "daily": round(v, 2), "adsets": 0} for c, v in camp_budget.items()}
    for c, sets in adset_budget.items():
        if c not in budgets and sets:
            budgets[c] = {"type": "ABO", "daily": round(sum(sets.values()), 2), "adsets": len(sets)}

    # ---- conta: limite de gastos, valor gasto e gasto ainda não cobrado (centavos) ----
    acc = acc_rows[0] if acc_rows else {}
    account = {
        "name": acc.get("account_name") or "Diálogo Engenharia e Construção",
        "spendCap": round(n(acc.get("spend_cap")) / 100, 2),
        "amountSpent": round(n(acc.get("amount_spent")) / 100, 2),
        "balance": round(n(acc.get("account_balance")) / 100, 2),
    }

    # ---- anúncios que mais trouxeram leads (mesmo anúncio em vários conjuntos = somado) ----
    def group_ads(rows):
        ads = {}
        for r in rows:
            c, name = r.get("campaign"), r.get("ad_name")
            if not c or not name:
                continue
            a = ads.setdefault((c, name), {"campaign": c, "name": name, "leads": 0, "spend": 0.0,
                                           "_best": -1, "link": None, "ig": None, "_thumb": None, "_id": None})
            sp, ld = n(r.get("spend")), int(n(r.get(LEAD_FIELD)))
            a["leads"] += ld
            a["spend"] += sp
            if sp > a["_best"]:
                a["_best"] = sp
                a["link"] = r.get("ad_preview_shareable_link") or a["link"]
                a["ig"] = r.get("instagram_permalink_url") or a["ig"]  # post do Instagram: o painel toca o vídeo
                a["_thumb"] = r.get("thumbnail_url") or a["_thumb"]
                a["_id"] = r.get("ad_id")
        return [a for a in ads.values() if a["leads"] > 0]

    keep_files = set()

    def publish(a):
        thumb = save_thumb(a["_thumb"], a["_id"] or (a["campaign"] + a["name"]))
        if thumb:
            keep_files.add(os.path.basename(thumb))
        return {"campaign": a["campaign"], "name": a["name"], "leads": a["leads"],
                "spend": round(a["spend"], 2), "link": a["link"], "ig": a["ig"], "thumb": thumb}

    rank = lambda xs: sorted(xs, key=lambda a: (-a["leads"], a["spend"]))
    top_ads = [publish(a) for a in rank(group_ads(ad_rows["last7"]))[:TOP_ADS]]
    ads_by_campaign = {}
    for key, rows in ad_rows.items():
        per = defaultdict(list)
        for a in group_ads(rows):
            per[a["campaign"]].append(a)
        ads_by_campaign[key] = {c: [publish(a) for a in rank(xs)[:TOP_ADS_PER_CAMPAIGN]]
                                for c, xs in per.items()}
    # apaga miniaturas que saíram dos rankings
    for f in glob.glob(os.path.join(ADS_DIR, "*")):
        if os.path.basename(f) not in keep_files:
            os.remove(f)

    out = {
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "today": today_s,
        "account": account,
        "daily": daily_out,
        "reach": reach,
        "breakdown": breakdown,
        "engagement": engagement,
        "budgets": budgets,
        "topAds": top_ads,
        "adsByCampaign": ads_by_campaign,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(daily_rows)} linhas diárias, {len(daily_out)} campanhas, {len(top_ads)} anúncios em metrics.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
