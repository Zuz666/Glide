"""The coach's query API over the knowledge layer: the live knowledge block for callouts, the Claude tools for
questions and reviews, and the grounding check of the coach's FACTS lines.

The LLM narrates; this module knows and computes. Every result is a compact JSON-serialisable dict of facts and
numbers with a "facts" list of the ids behind them. Nothing here writes to kb.db and nothing raises to the coach.

  pack = KnowledgePack(kb, rxp=None, geo=None)
      kb    store.KB, a path, or None (missing or unloadable: pack.ok is False; fact queries answer
            {"unavailable": reason}, verified_step answers {"status": "missing"})
      rxp   RestedXP settings of the character; they decide which steps exist and how RXP numbers them:
            ssf, group_quests, hardcore, dungeons. Default DEFAULT_RXP (solo self-found, group quests on: how
            the player's characters are set up in RestedXP's SavedVariables)
      geo   optional companion geo.Geo, for directions like "Kharanos, near Innkeeper Belm, 80 yd north"
  ctx = pack.context(summary, coach_flags=None, triggers=None, book=None, recent=None, now=None, me=None)
      the live context of one coach turn: GameState.summary(), the player, the resolved guide key / route /
      step, muted commentary types, companion bookkeeping (deaths, mount, group, step visits), the recent coach
      log. Tools and the block take it.
  pack.block(ctx, triggers=None)   the "knowledge" block for live callouts (<= BLOCK_TOKENS, measured len/4)
  pack.panel(summary=None)         {"why", "pace"} for the dashboard, from the last block (never computes)
  pack.decay_info(quest_id, level) turnin_decay_soon facts (one cached row per quest; cheap on any thread)
  pack.verify_facts(ids)           (known, unknown): grounding check of a FACTS line
  pack.status()                    KB status for the companion's startup line
  pack.verified_step(summary)      {"status", "step"?}: exact source ordinal/block hash match for one authored
                                  main instruction and separate notes; never uses visible-order heuristics
  TOOLS / tool_definitions()       the Messages API "tools" list
  dispatch(name, args, ctx)        one tool's result dict (time-budgeted, trimmed to RESULT_TOKENS, never raises)

Guide and step resolution. The live G page carries RestedXP's own guide name (zero-padded and cut to 40 bytes:
'05-11 Dun Morogh') and its group ('RestedXP Forever Guide (A)'); rxp_guide.key is 'group||name' with the
unpadded name. GuideResolver normalises both forms (sources/live_logs.Guides: rxp_parser.affix_name, zero padding,
the 40-byte cut), prefers the page's group, the character's faction and non-dungeon guides, and caches. The live
step number (core `st`) is RestedXP's number among the steps it keeps for these settings: route.rxp_to_idx (the
mapping route.brief_for_rxp_step uses) turns it into the guide's step idx. When the live S0 page lists quests the
mapped step does not touch, the nearest step within 6 numbers that touches them wins.
Full authored current titles use verified_step instead: core `si` is the original ordinal in that step's own
source guide; `sh` fingerprints only its canonical source block. The live `st` remains the displayed position,
not a lookup key. Match the pair in the current immutable pack, independently of G metadata or the browser's
selected guide; preserve applicable inline conditions and separate notes. Other steps, guide headers and native
visible ordering are not compared or reconstructed. Lookahead remains short live text until it becomes current.
GameState supplies race/faction and fallback class only from B received in the current source-pointer/class
cohort. Pointer changes, detected restarts and stale capture gaps revoke that evidence before the next frame
is published; a B in the transition frame is accepted as fresh. This prevents a relog from verifying the
previous character's inline branch. Missing required player evidence keeps the live short text.

Fact ids: q:<quest> c:<creature> i:<item> p:<param key> h:<heuristic id> s:<guide name or key>/<step idx>
poi:<kind>:<id> g:<glossary term>. verify_facts() accepts one only when the row exists and is usable.

Modules: pack (KnowledgePack, a facade over resolve, lookup, panel, extras and facts), context (Ctx), guides
(guide and player resolution), results (budgets, trimming, fact ids), reasons (why codes in words), community
(community notes), navigation (walking and flying routes; walk_world for routes outside the coach), explorer
(per-guide facts for the Guides page), tools (the Claude tools and dispatch), values (small shared helpers), _layer
(the defensive imports).
"""
from knowledge.api._layer import IMPORT_ERROR
from knowledge.api.community import NOTE_SKIP, community_notes
from knowledge.api.context import Ctx
from knowledge.api.guides import (DEFAULT_RXP, HORDE_RACES, RXP_KEYS, GuideResolver, norm_guide_name, player_from,
                                  resolve_guide_key)
from knowledge.api.navigation import flight_from_track, plan_to_step, walk_route, walk_to_step, walk_world
from knowledge.api.pack import KnowledgePack, verify_facts
from knowledge.api.reasons import WHY_NOT_TEXT, WHY_TEXT, why_text
from knowledge.api.results import BLOCK_TOKENS, FACT_RE, RESULT_TOKENS, approx_tokens, fit_block, trim_result
from knowledge.api.tools import TOOL_BUDGET_S, TOOLS, dispatch, tool_definitions, tool_names
from knowledge.api.tools.advice import t_community, t_heuristics
from knowledge.api.tools.lookup import t_creature_info, t_glossary, t_quest_info, t_search
from knowledge.api.tools.mechanics import t_kill_xp, t_param, t_quest_xp
from knowledge.api.tools.route import route_steps, t_guide_summary, t_route_lookahead, t_step_brief, t_why_this_step
from knowledge.api.tools.session import t_recent_log, t_session_review
from knowledge.api.tools.simulation import t_alternatives_here, t_eta, t_skip_impact, t_what_if
from knowledge.api.tools.travel import t_path_time, t_walk_route
from knowledge.api.values import COMPASS, CONFIRMED, RANK, compass, money_text

__all__ = ["IMPORT_ERROR", "NOTE_SKIP", "community_notes", "Ctx", "DEFAULT_RXP", "HORDE_RACES", "RXP_KEYS",
           "GuideResolver", "norm_guide_name", "player_from", "resolve_guide_key", "flight_from_track", "plan_to_step",
           "walk_route", "walk_to_step", "walk_world", "KnowledgePack", "verify_facts", "WHY_NOT_TEXT", "WHY_TEXT",
           "why_text",
           "BLOCK_TOKENS", "FACT_RE", "RESULT_TOKENS", "approx_tokens", "fit_block", "trim_result", "TOOL_BUDGET_S",
           "TOOLS", "dispatch", "tool_definitions", "tool_names", "t_community", "t_heuristics", "t_creature_info",
           "t_glossary", "t_quest_info", "t_search", "t_kill_xp", "t_param", "t_quest_xp", "route_steps",
           "t_guide_summary", "t_route_lookahead", "t_step_brief", "t_why_this_step", "t_recent_log",
           "t_session_review", "t_alternatives_here", "t_eta", "t_skip_impact", "t_what_if", "t_path_time",
           "t_walk_route", "COMPASS", "CONFIRMED", "RANK", "compass", "money_text"]

_flight_from_track = flight_from_track       # the name tests/test_navmesh.py knows it by
