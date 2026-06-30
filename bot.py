import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta

# ── Secrets ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_KEY      = os.environ["SUPABASE_KEY"]

# ── Config ────────────────────────────────────────────────────────────────────
MIN_EDGE_PCT      = 8.0    # only alert if Claude's edge > 8% vs market
MAX_SIGNALS       = 8      # max signals per run (avoid Telegram spam)
MIN_BET_CONFIDENCE = 7     # Claude confidence threshold (1-10)

# ── Azuro GraphQL (public, no key needed) ─────────────────────────────────────
AZURO_SUBGRAPH = "https://thegraph-1.onchainfeed.org/subgraphs/name/azuro-protocol/azuro-data-feed-polygon"

def debug_raw_query():
    """Zero-filter query to see what's actually in the subgraph right now."""
    query = """
    {
      games(first: 5, orderBy: startsAt, orderDirection: desc, subgraphError: allow) {
        id
        gameId
        title
        startsAt
        state
      }
    }
    """
    try:
        r = requests.post(AZURO_SUBGRAPH, json={"query": query},
            headers={"Content-Type": "application/json"}, timeout=15)
        data = r.json()
        print(f"  🔍 DEBUG raw query result: {json.dumps(data)[:800]}")
    except Exception as e:
        print(f"  🔍 DEBUG query failed: {e}")


def fetch_upcoming_games():
    """Fetch upcoming sports games from Azuro's V3 data-feed subgraph.
    V3 GameState enum values are: Finished, Live, Prematch, Stopped
    Country is at the same level as league (not nested under it).
    """
    now_ts    = int(datetime.now(timezone.utc).timestamp())
    cutoff_ts = now_ts + (72 * 3600)  # next 72 hours

    # First, run a zero-filter debug query to see what's actually there
    debug_raw_query()

    query = """
    {
      games(
        first: 50
        where: {
          startsAt_gt: "%s"
          startsAt_lt: "%s"
        }
        orderBy: startsAt
        orderDirection: asc
        subgraphError: allow
      ) {
        id
        gameId
        slug
        title
        startsAt
        state
        sport { name }
        league { name }
        country { name }
        participants { name image }
        conditions {
          conditionId
          outcomes {
            outcomeId
            currentOdds
          }
        }
      }
    }
    """ % (now_ts, cutoff_ts)

    r = requests.post(AZURO_SUBGRAPH,
        json={"query": query},
        headers={"Content-Type": "application/json"},
        timeout=15)
    r.raise_for_status()
    data = r.json()

    if "errors" in data:
        print(f"  ⚠ GraphQL errors: {data['errors']}")
        return []

    games = data.get("data", {}).get("games", [])
    print(f"  Raw games returned: {len(games)}")
    if games:
        print(f"  Sample game states: {[g.get('state') for g in games[:5]]}")

    games_with_odds = [g for g in games if g.get("conditions")]
    print(f"  Games with conditions: {len(games_with_odds)}")

    return games_with_odds


def fetch_upcoming_games_fallback(now_ts, cutoff_ts):
    """Looser query with minimal filters as a last resort."""
    query = """
    {
      games(
        first: 50
        where: {
          startsAt_gt: "%s"
          startsAt_lt: "%s"
        }
        orderBy: startsAt
        orderDirection: asc
        subgraphError: allow
      ) {
        id
        gameId
        slug
        title
        startsAt
        state
        sport { name }
        league { name }
        country { name }
        participants { name image }
        conditions {
          conditionId
          outcomes {
            outcomeId
            currentOdds
          }
        }
      }
    }
    """ % (now_ts, cutoff_ts)

    r = requests.post(AZURO_SUBGRAPH,
        json={"query": query},
        headers={"Content-Type": "application/json"},
        timeout=15)
    r.raise_for_status()
    data = r.json()

    if "errors" in data:
        print(f"  ⚠ Fallback GraphQL errors: {data['errors']}")
        return []

    games = data.get("data", {}).get("games", [])
    print(f"  Fallback raw games: {len(games)}")
    games_with_odds = [g for g in games if g.get("conditions")]
    print(f"  Fallback games with conditions: {len(games_with_odds)}")
    return games_with_odds

def parse_game(game):
    """Extract clean match data from raw Azuro game object."""
    participants = game.get("participants", [])
    home = participants[0]["name"] if len(participants) > 0 else "Team A"
    away = participants[1]["name"] if len(participants) > 1 else "Team B"

    # Get moneyline odds (first condition, outcomes 0/1/2 = home/draw/away)
    conditions = game.get("conditions", [])
    home_odds = draw_odds = away_odds = None
    condition_id = None

    for cond in conditions:
        outcomes = cond.get("outcomes", [])
        if len(outcomes) >= 2:
            condition_id = cond["conditionId"]
            # Azuro odds are multipliers (e.g. 2.5 = 2.5x payout)
            if len(outcomes) == 2:
                home_odds = float(outcomes[0]["currentOdds"])
                away_odds = float(outcomes[1]["currentOdds"])
            elif len(outcomes) >= 3:
                home_odds = float(outcomes[0]["currentOdds"])
                draw_odds = float(outcomes[1]["currentOdds"])
                away_odds = float(outcomes[2]["currentOdds"])
            break

    if not home_odds or not away_odds:
        return None

    # Convert odds to implied probabilities
    def odds_to_prob(odds):
        return round(100 / odds, 1) if odds > 0 else None

    starts_at = datetime.fromtimestamp(int(game["startsAt"]), tz=timezone.utc)
    time_until = starts_at - datetime.now(timezone.utc)
    hours_until = round(time_until.total_seconds() / 3600, 1)

    return {
        "game_id":      game["gameId"],
        "condition_id": condition_id,
        "slug":         game.get("slug", ""),
        "home":         home,
        "away":         away,
        "sport":        game.get("sport", {}).get("name", "Unknown"),
        "league":       game.get("league", {}).get("name", "Unknown"),
        "country":      game.get("country", {}).get("name", ""),
        "starts_at":    starts_at.strftime("%Y-%m-%d %H:%M UTC"),
        "hours_until":  hours_until,
        "home_odds":    home_odds,
        "draw_odds":    draw_odds,
        "away_odds":    away_odds,
        "home_prob":    odds_to_prob(home_odds),
        "draw_prob":    odds_to_prob(draw_odds) if draw_odds else None,
        "away_prob":    odds_to_prob(away_odds),
    }

def get_news_context(home, away, sport, league):
    """Fetch any recent news about the teams."""
    try:
        r = requests.get(
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5)
        # CoinDesk won't have sports news — return generic context
    except:
        pass
    return f"{home} vs {away} in {league}"

def ask_claude_for_signal(match):
    """Ask Claude to analyse the match and estimate true probabilities."""
    has_draw = match["draw_odds"] is not None
    draw_line = f"Draw: {match['draw_odds']}x (implied {match['draw_prob']}%)" if has_draw else ""

    prompt = f"""You are an expert sports analyst and betting strategist. Analyse this match and estimate the TRUE win probabilities.

Match: {match['home']} vs {match['away']}
Sport: {match['sport']}
League: {match['league']} ({match['country']})
Kickoff: {match['starts_at']} ({match['hours_until']} hours away)

Market Odds (Azuro Protocol):
- {match['home']} Win: {match['home_odds']}x (market implies {match['home_prob']}%)
{f"- Draw: {match['draw_odds']}x (market implies {match['draw_prob']}%)" if has_draw else ""}
- {match['away']} Win: {match['away_odds']}x (market implies {match['away_prob']}%)

Based on your knowledge of these teams, their recent form, head-to-head record, and the current tournament context (if applicable):

1. Estimate the TRUE probability for each outcome
2. Identify if there is a POSITIVE EDGE (your estimate > market implied probability)
3. Recommend the BEST BET if edge > {MIN_EDGE_PCT}%

Consider:
- Team quality and current form
- Home/away advantage
- Tournament stakes and motivation
- Any known injuries or suspensions to key players
- Historical head-to-head records

Respond ONLY with JSON:
{{
  "home_true_prob": number,
  "draw_true_prob": number or null,
  "away_true_prob": number,
  "best_bet": "{match['home']}" | "Draw" | "{match['away']}" | "SKIP",
  "best_bet_edge": number,
  "confidence": 1-10,
  "reasoning": "2-3 sentence explanation of key factors",
  "key_factors": ["factor1", "factor2", "factor3"],
  "risk_level": "LOW" | "MEDIUM" | "HIGH"
}}"""

    r = requests.post("https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key":         ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        json={
            "model":      "claude-haiku-4-5-20251001",
            "max_tokens": 400,
            "messages":   [{"role": "user", "content": prompt}],
        },
        timeout=30)
    r.raise_for_status()
    raw = r.json()["content"][0]["text"].strip()
    raw = raw.replace("```json","").replace("```","").strip()
    return json.loads(raw)

def telegram(message):
    """Send Telegram message."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10)
        if r.status_code == 200:
            print("  📱 Telegram sent")
        else:
            print(f"  ⚠ Telegram: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"  ⚠ Telegram error: {e}")

def save_signal(match, analysis):
    """Save signal to Supabase for tracking."""
    try:
        payload = {
            "game_id":      match["game_id"],
            "home":         match["home"],
            "away":         match["away"],
            "sport":        match["sport"],
            "league":       match["league"],
            "starts_at":    match["starts_at"],
            "best_bet":     analysis["best_bet"],
            "edge_pct":     analysis["best_bet_edge"],
            "confidence":   analysis["confidence"],
            "reasoning":    analysis["reasoning"],
            "home_odds":    match["home_odds"],
            "away_odds":    match["away_odds"],
            "draw_odds":    match.get("draw_odds"),
            "home_true_prob": analysis["home_true_prob"],
            "away_true_prob": analysis["away_true_prob"],
            "risk_level":   analysis["risk_level"],
            "status":       "pending",
            "created_at":   datetime.now(timezone.utc).isoformat(),
        }
        r = requests.post(f"{SUPABASE_URL}/rest/v1/signals",
            headers={
                "apikey":        SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type":  "application/json",
                "Prefer":        "return=minimal",
            },
            json=payload, timeout=10)
        if r.status_code in (200, 201):
            print(f"  ✅ Signal saved to Supabase")
        else:
            print(f"  ⚠ Supabase save failed: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"  ⚠ Signal save error: {e}")

def already_ran_recently(minutes=50):
    """Prevent duplicate runs."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/signals?order=created_at.desc&limit=1",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=10)
        data = r.json()
        if isinstance(data, list) and data:
            last = datetime.fromisoformat(data[0]["created_at"].replace("Z", "+00:00"))
            diff = (datetime.now(timezone.utc) - last).total_seconds() / 60
            if diff < minutes:
                print(f"  ⏭ Already ran {diff:.0f} mins ago — skipping")
                return True
    except Exception as e:
        print(f"  ⚠ Duplicate check: {e}")
    return False

def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*55}")
    print(f"  Sports Signal Bot  |  {now}")
    print(f"  Powered by Azuro Protocol + Claude AI")
    print(f"{'='*55}\n")

    if already_ran_recently(minutes=50):
        return

    # Fetch upcoming games
    print("  ⚽ Fetching upcoming games from Azuro...")
    try:
        raw_games = fetch_upcoming_games()
        print(f"  Found {len(raw_games)} upcoming games\n")
    except Exception as e:
        print(f"  ❌ Failed to fetch games: {e}")
        telegram(f"⚠️ <b>Sports Signal Bot Error</b>\nCouldn't fetch games: {e}")
        return

    if not raw_games:
        print("  No upcoming games found in next 24 hours")
        return

    # Parse games
    games = []
    for g in raw_games:
        parsed = parse_game(g)
        if parsed:
            games.append(parsed)

    print(f"  Parsed {len(games)} valid games with odds\n")

    signals    = []
    skipped    = 0
    analysed   = 0

    for game in games[:15]:  # analyse max 15 games per run
        print(f"── {game['home']} vs {game['away']} ({game['sport']}) ──")
        print(f"   {game['league']} | In {game['hours_until']}h")
        print(f"   Odds: {game['home']} {game['home_odds']}x | {game['away']} {game['away_odds']}x")

        try:
            analysis = ask_claude_for_signal(game)
            analysed += 1

            edge     = analysis.get("best_bet_edge", 0)
            bet      = analysis.get("best_bet", "SKIP")
            conf     = analysis.get("confidence", 0)
            risk     = analysis.get("risk_level", "MEDIUM")
            reason   = analysis.get("reasoning", "")

            print(f"   Claude: {bet}  |  Edge: {edge:+.1f}%  |  Conf: {conf}/10  |  Risk: {risk}")
            print(f"   → {reason[:80]}...")

            if bet != "SKIP" and edge >= MIN_EDGE_PCT and conf >= MIN_BET_CONFIDENCE:
                print(f"   ✅ SIGNAL FOUND!")
                signals.append({"game": game, "analysis": analysis})
                save_signal(game, analysis)
            else:
                skipped += 1
                print(f"   ⏭ Skipped (edge too low or confidence too low)")

        except Exception as e:
            print(f"   ❌ Analysis error: {e}")

        print()
        time.sleep(2)  # avoid rate limits

    # ── Send Telegram summary ─────────────────────────────────────────────────
    if not signals:
        msg = (
            f"🔍 <b>Sports Signal Bot</b>\n"
            f"🕐 {now}\n\n"
            f"📊 Analysed {analysed} games\n"
            f"⏭ No strong edges found this run\n\n"
            f"<i>Minimum edge required: {MIN_EDGE_PCT}% | Confidence: {MIN_BET_CONFIDENCE}/10+</i>"
        )
        telegram(msg)
        print("No signals found this run — Telegram notified")
        return

    # Send header
    telegram(
        f"⚽ <b>Sports Signal Bot</b>\n"
        f"🕐 {now}\n\n"
        f"📊 Analysed {analysed} games  |  Found <b>{len(signals)} edge{'s' if len(signals)>1 else ''}</b>\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

    time.sleep(1)

    # Send each signal
    risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}

    for s in signals[:MAX_SIGNALS]:
        g   = s["game"]
        a   = s["analysis"]
        bet = a["best_bet"]
        edge = a["best_bet_edge"]
        conf = a["confidence"]
        risk = a["risk_level"]

        # Figure out the odds for the recommended bet
        if bet == g["home"]:
            bet_odds = g["home_odds"]
            market_prob = g["home_prob"]
            true_prob   = a["home_true_prob"]
        elif bet == g["away"]:
            bet_odds = g["away_odds"]
            market_prob = g["away_prob"]
            true_prob   = a["away_true_prob"]
        else:
            bet_odds = g.get("draw_odds", 0)
            market_prob = g.get("draw_prob", 0)
            true_prob   = a.get("draw_true_prob", 0)

        # Build key factors string
        factors = a.get("key_factors", [])
        factors_str = "\n".join(f"  • {f}" for f in factors[:3])

        msg = (
            f"🎯 <b>SIGNAL FOUND</b>  {risk_emoji.get(risk, '🟡')} {risk} RISK\n\n"
            f"⚽ <b>{g['home']} vs {g['away']}</b>\n"
            f"🏆 {g['league']} ({g['country']})\n"
            f"🕐 {g['starts_at']} (in {g['hours_until']}h)\n\n"
            f"💡 <b>BET: {bet}</b>\n"
            f"💰 Odds: <b>{bet_odds}x</b> (pays {bet_odds}x your stake)\n\n"
            f"📊 <b>Edge Analysis:</b>\n"
            f"  Market implies: {market_prob}%\n"
            f"  Claude estimates: {true_prob}%\n"
            f"  Edge: <b>+{edge:.1f}%</b> ← this is your advantage\n\n"
            f"🎯 Confidence: {conf}/10\n\n"
            f"🧠 <b>Why:</b>\n{a['reasoning']}\n\n"
            f"📌 Key factors:\n{factors_str}\n\n"
            f"🌐 Bet on Future.news or any Azuro frontend\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        telegram(msg)
        time.sleep(1)

    print(f"\n✅ Run complete — {len(signals)} signals sent\n")

if __name__ == "__main__":
    main()
