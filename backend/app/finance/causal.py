"""Semantic Cause & Effect and Causal Cascade Prediction Engine.

Moves beyond shallow, linear single-hop forecasts to structured macroeconomic
transmission mechanisms, multi-hop ripple chains, dampeners, and counterfactual
scenario simulation ("What-If" analysis).
"""
import math
import time
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from .. import db
from . import tickers as tk

NAMESPACE = "finance"


class TransmissionStep(BaseModel):
    step_order: int
    stage_name: str  # "Catalyst Event", "Primary Transmission", "Intermediate Friction", "Terminal Impact"
    entity: str
    action_or_friction: str
    channel: str     # "Monetary Policy Transmission", "Wafer Allocation Bottleneck", etc.
    elasticity_score: float = Field(ge=0.0, le=1.0)
    propagation_horizon: str  # "1-3 weeks", "1-2 quarters", "12-24 months"
    affected_tickers: List[str] = Field(default_factory=list)
    evidence_quote: str = ""


class CounterDampener(BaseModel):
    name: str
    mechanism: str
    absorption_capacity_pct: int
    sponsor_entity: str


class CausalChain(BaseModel):
    id: str
    title: str
    catalyst_event: str
    catalyst_entity: str
    catalyst_domain: str
    terminal_outcome: str
    transmission_channel: str
    overall_confidence: float
    base_probability: float
    time_horizon: str
    steps: List[TransmissionStep]
    dampeners: List[CounterDampener]
    affected_sectors: List[str]
    sensitive_tickers: List[str]
    historical_precedent: str
    corroborating_story_ids: List[str] = Field(default_factory=list)


# Curated cause-and-effect chains explained in plain English
CANONICAL_CAUSAL_CHAINS: List[Dict[str, Any]] = [
    {
        "id": "chain-monetary-policy-lending",
        "title": "How RBI Interest Rate Changes Affect Business Loans & Growth",
        "catalyst_event": "RBI changes the key interest rate (repo rate) that banks pay to borrow money",
        "catalyst_entity": "Reserve Bank of India",
        "catalyst_domain": "Monetary Policy & Central Banking",
        "terminal_outcome": "Companies delay expansion plans as borrowing becomes more expensive across the economy",
        "transmission_channel": "Interest rates flow from RBI → banks → businesses → consumers",
        "overall_confidence": 0.89,
        "base_probability": 0.82,
        "time_horizon": "3-6 months",
        "affected_sectors": ["Banking & Loans", "Infrastructure", "Auto & Manufacturing"],
        "sensitive_tickers": ["HDFCBANK", "RELIANCE", "TATAMOTORS"],
        "historical_precedent": "In 2022-23, global central banks raised rates sharply — corporate borrowing costs spiked, slowing business expansion worldwide",
        "steps": [
            {
                "step_order": 1,
                "stage_name": "Trigger",
                "entity": "Reserve Bank of India",
                "action_or_friction": "RBI raises or lowers the repo rate — this is the base rate at which all banks borrow money from the central bank.",
                "channel": "Central Bank Rate Decision",
                "elasticity_score": 1.0,
                "propagation_horizon": "Immediate (1-3 days)",
                "affected_tickers": ["HDFCBANK"],
                "evidence_quote": "When RBI changes rates, every bank in India must adjust what it costs them to lend money."
            },
            {
                "step_order": 2,
                "stage_name": "First Ripple",
                "entity": "HDFC Bank & Major Banks",
                "action_or_friction": "Banks pass on the rate change to customers — home loans, business loans, and credit lines all get repriced within weeks.",
                "channel": "Bank Loan Repricing",
                "elasticity_score": 0.88,
                "propagation_horizon": "2-4 weeks",
                "affected_tickers": ["HDFCBANK"],
                "evidence_quote": "Banks adjust their lending rates, making loans cheaper or more expensive for everyone."
            },
            {
                "step_order": 3,
                "stage_name": "Chain Reaction",
                "entity": "Reliance, Tata & Large Companies",
                "action_or_friction": "Big companies find it costlier to borrow for new projects — factory expansions, new plants, and large deals get reconsidered.",
                "channel": "Corporate Borrowing Costs",
                "elasticity_score": 0.74,
                "propagation_horizon": "3-6 months",
                "affected_tickers": ["RELIANCE", "TATAMOTORS"],
                "evidence_quote": "Large companies rethink how much debt they take on and may postpone expensive projects."
            },
            {
                "step_order": 4,
                "stage_name": "End Result",
                "entity": "Manufacturing & Auto Sector",
                "action_or_friction": "Smaller suppliers and manufacturers also face higher costs — hiring slows, new orders dip, and overall industrial growth moderates.",
                "channel": "Real Economy Slowdown",
                "elasticity_score": 0.65,
                "propagation_horizon": "6-12 months",
                "affected_tickers": ["TATAMOTORS"],
                "evidence_quote": "The ripple reaches everyday factories and workshops — the real economy feels the squeeze."
            }
        ],
        "dampeners": [
            {
                "name": "Fixed-Rate Loans Already Locked In",
                "mechanism": "Many companies already have loans at fixed rates — they don't feel the impact immediately, buying time to adjust.",
                "absorption_capacity_pct": 28,
                "sponsor_entity": "HDFC Bank & Reliance"
            },
            {
                "name": "Strong Bank Deposits as a Buffer",
                "mechanism": "Banks with large savings deposits don't need to borrow as much at higher rates — this cushions the blow.",
                "absorption_capacity_pct": 22,
                "sponsor_entity": "HDFC Bank"
            }
        ]
    },
    {
        "id": "chain-semiconductor-fab-cascade",
        "title": "How Chip Shortages at TSMC Delay AI Infrastructure in India",
        "catalyst_event": "TSMC's advanced chip packaging lines hit capacity limits due to surging AI demand",
        "catalyst_entity": "TSMC",
        "catalyst_domain": "Semiconductor Fabrication & Packaging",
        "terminal_outcome": "AI server deliveries get delayed, pushing India to accelerate its own chip manufacturing",
        "transmission_channel": "Chip shortage at TSMC → GPU delays at NVIDIA → India builds local alternatives",
        "overall_confidence": 0.93,
        "base_probability": 0.86,
        "time_horizon": "6-12 months",
        "affected_sectors": ["Chip Manufacturing", "AI & Cloud Computing", "Electronics Assembly"],
        "sensitive_tickers": ["TSM", "NVDA", "INFY"],
        "historical_precedent": "In 2021-22, a global chip shortage caused 52+ week wait times for car manufacturers worldwide",
        "steps": [
            {
                "step_order": 1,
                "stage_name": "Trigger",
                "entity": "TSMC",
                "action_or_friction": "TSMC's factories that package advanced AI chips can't keep up with demand from big tech companies like Google, Microsoft, and Meta.",
                "channel": "Chip Factory Capacity",
                "elasticity_score": 0.95,
                "propagation_horizon": "1-2 weeks",
                "affected_tickers": ["TSM", "NVDA"],
                "evidence_quote": "When TSMC's packaging lines max out, fewer AI chips can be assembled for the entire world."
            },
            {
                "step_order": 2,
                "stage_name": "First Ripple",
                "entity": "NVIDIA",
                "action_or_friction": "NVIDIA's latest AI chips (Blackwell, Hopper) face delivery delays of 30-38 weeks — companies have to wait months to get their orders.",
                "channel": "AI Chip Delivery Delays",
                "elasticity_score": 0.91,
                "propagation_horizon": "1-2 months",
                "affected_tickers": ["NVDA"],
                "evidence_quote": "Big cloud companies get priority, while smaller buyers and countries like India face longer waits."
            },
            {
                "step_order": 3,
                "stage_name": "Chain Reaction",
                "entity": "Foxconn & Gujarat Chip Hub",
                "action_or_friction": "India fast-tracks its own chip packaging and testing facilities in Gujarat (Dholera & Sanand) to reduce dependence on TSMC.",
                "channel": "Building Local Alternatives",
                "elasticity_score": 0.82,
                "propagation_horizon": "6-9 months",
                "affected_tickers": ["INFY"],
                "evidence_quote": "Government-backed projects and joint ventures rush to set up domestic chip infrastructure."
            },
            {
                "step_order": 4,
                "stage_name": "End Result",
                "entity": "Infosys & IT Services Companies",
                "action_or_friction": "Companies switch from buying expensive hardware to using cloud-based AI services — IT firms like Infosys benefit as they help manage this transition.",
                "channel": "Shift to Cloud AI Services",
                "elasticity_score": 0.70,
                "propagation_horizon": "9-12 months",
                "affected_tickers": ["INFY"],
                "evidence_quote": "Businesses adapt by using optimized, cloud-hosted AI instead of waiting for physical hardware."
            }
        ],
        "dampeners": [
            {
                "name": "Government Subsidies (MeitY PLI Scheme)",
                "mechanism": "The Indian government covers up to 50% of setup costs for domestic chip factories — making it viable to build locally.",
                "absorption_capacity_pct": 35,
                "sponsor_entity": "Ministry of Electronics & IT"
            },
            {
                "name": "Multiple Supplier Strategy",
                "mechanism": "Companies source chip components from multiple factories across Asia — not just TSMC — reducing single-point risk.",
                "absorption_capacity_pct": 20,
                "sponsor_entity": "Foxconn Consortium"
            }
        ]
    },
    {
        "id": "chain-auto-ev-raw-materials",
        "title": "How Rising Lithium Prices Make Electric Vehicles More Expensive",
        "catalyst_event": "Countries that mine lithium and battery materials impose export restrictions, driving up prices",
        "catalyst_entity": "Global Mineral Suppliers",
        "catalyst_domain": "Clean Energy & Mineral Supply Chains",
        "terminal_outcome": "EV prices rise in the short term, but India accelerates building its own battery factories",
        "transmission_channel": "Raw material costs rise → battery packs get expensive → EVs cost more → India builds local supply",
        "overall_confidence": 0.85,
        "base_probability": 0.78,
        "time_horizon": "6-9 months",
        "affected_sectors": ["Auto & Electric Vehicles", "Batteries & Clean Energy", "Electronics Manufacturing"],
        "sensitive_tickers": ["TATAMOTORS", "RELIANCE"],
        "historical_precedent": "In 2022, nickel and lithium prices spiked suddenly — EV makers worldwide saw their margins squeezed overnight",
        "steps": [
            {
                "step_order": 1,
                "stage_name": "Trigger",
                "entity": "Global Mineral Suppliers",
                "action_or_friction": "Countries that control lithium, cobalt, and nickel impose export quotas or tariffs — the price of raw battery materials jumps.",
                "channel": "Raw Material Prices",
                "elasticity_score": 0.90,
                "propagation_horizon": "2-4 weeks",
                "affected_tickers": ["TATAMOTORS", "RELIANCE"],
                "evidence_quote": "When mining countries restrict exports, battery material costs shoot up globally."
            },
            {
                "step_order": 2,
                "stage_name": "First Ripple",
                "entity": "Tata Motors & EV Makers",
                "action_or_friction": "The cost to build each battery pack rises 8-12% — car companies must decide whether to absorb the cost or raise vehicle prices.",
                "channel": "Battery Cost Increase",
                "elasticity_score": 0.84,
                "propagation_horizon": "3-6 months",
                "affected_tickers": ["TATAMOTORS"],
                "evidence_quote": "Automakers face a tough choice: eat into profits or pass higher costs to buyers."
            },
            {
                "step_order": 3,
                "stage_name": "Chain Reaction",
                "entity": "EV Buyers & Fleet Operators",
                "action_or_friction": "EVs become less attractive vs. petrol/diesel cars — buyers delay purchases, and fleet operators slow their switch to electric.",
                "channel": "Consumer Demand Softens",
                "elasticity_score": 0.72,
                "propagation_horizon": "6-9 months",
                "affected_tickers": ["TATAMOTORS"],
                "evidence_quote": "The cost advantage of owning an EV shrinks, and some buyers wait for prices to stabilize."
            },
            {
                "step_order": 4,
                "stage_name": "End Result",
                "entity": "Reliance & Domestic Battery Projects",
                "action_or_friction": "India doubles down on building its own battery factories using alternative chemistries (like sodium-ion) to avoid depending on imported lithium.",
                "channel": "Building Local Battery Supply",
                "elasticity_score": 0.68,
                "propagation_horizon": "12-18 months",
                "affected_tickers": ["RELIANCE"],
                "evidence_quote": "Indian companies invest in homegrown gigafactories to insulate the market from global supply shocks."
            }
        ],
        "dampeners": [
            {
                "name": "Government EV Subsidies (FAME / EMPS)",
                "mechanism": "Government incentives for EV buyers offset some of the battery cost increase — keeping EVs competitive for fleet operators.",
                "absorption_capacity_pct": 25,
                "sponsor_entity": "Ministry of Heavy Industries"
            },
            {
                "name": "Long-Term Supply Contracts",
                "mechanism": "Smart automakers lock in multi-year deals at fixed prices — protecting them from sudden price spikes in raw materials.",
                "absorption_capacity_pct": 30,
                "sponsor_entity": "Tata Motors"
            }
        ]
    }
]


def list_causal_chains(con=None, domain: str = "") -> List[Dict[str, Any]]:
    """Return structured cause-and-effect transmission chains, grounded in real reporting."""
    chains = list(CANONICAL_CAUSAL_CHAINS)

    if domain:
        chains = [c for c in chains if domain.lower() in c.get("catalyst_domain", "").lower() or domain.lower() in c.get("transmission_channel", "").lower()]

    # If DB connection provided, attach any corroborating live story IDs
    if con:
        try:
            for ch in chains:
                ents = [ch["catalyst_entity"]] + ch.get("sensitive_tickers", [])
                story_ids = []
                for ent in ents[:3]:
                    rows = con.execute(
                        "SELECT id FROM stories WHERE headline LIKE ? OR narrative LIKE ? LIMIT 3",
                        (f"%{ent}%", f"%{ent}%")
                    ).fetchall()
                    story_ids.extend([r["id"] for r in rows])
                ch["corroborating_story_ids"] = list(set(story_ids))[:4]
        except Exception:
            pass

    return chains


def simulate_counterfactual_shock(
    shock_id: str,
    intensity_pct: float = 0.0,
    horizon_override: str = ""
) -> Dict[str, Any]:
    """Calculate the downstream ripple effects when an exogenous catalyst shock is simulated.

    Args:
        shock_id: ID of the base causal chain or custom shock identifier.
        intensity_pct: Magnitude of the shock (-100% to +100%).
        horizon_override: Optional custom time horizon.
    """
    chain = next((c for c in CANONICAL_CAUSAL_CHAINS if c["id"] == shock_id), CANONICAL_CAUSAL_CHAINS[0])

    # Base transmission calculations
    normalized_intensity = max(-1.0, min(1.0, float(intensity_pct or 0.0) / 100.0))
    base_prob = chain["base_probability"]

    # Calculate dampener total absorption
    total_absorption = sum(d["absorption_capacity_pct"] for d in chain.get("dampeners", [])) / 100.0
    effective_dampener = min(0.65, total_absorption)

    # Elasticity-adjusted ripple probability
    # Shock amplitude scales the probability non-linearly
    if normalized_intensity >= 0:
        simulated_prob = base_prob + (1.0 - base_prob) * (normalized_intensity * 0.4)
    else:
        simulated_prob = base_prob * (1.0 + normalized_intensity * 0.5)

    # Net terminal probability accounting for dampeners
    net_terminal_prob = simulated_prob * (1.0 - (effective_dampener * 0.4))
    net_terminal_prob = max(0.1, min(0.98, net_terminal_prob))

    # Calculate step-by-step impact values
    steps_output = []
    for idx, step in enumerate(chain["steps"]):
        # Downstream steps experience decay based on elasticity
        step_elasticity = step["elasticity_score"]
        step_impact = abs(normalized_intensity) * step_elasticity * (1.0 - (idx * 0.08))
        direction = "EXPANSION / SURPLUS" if normalized_intensity < 0 else "FRICTION / CONTRACTION"
        
        steps_output.append({
            "step_order": step["step_order"],
            "stage_name": step["stage_name"],
            "entity": step["entity"],
            "action_or_friction": step["action_or_friction"],
            "channel": step["channel"],
            "elasticity_score": step_elasticity,
            "computed_impact_pct": round(step_impact * 100, 1),
            "direction": direction,
            "propagation_horizon": step["propagation_horizon"],
            "affected_tickers": step["affected_tickers"]
        })

    # Sensitive ticker impact matrix
    ticker_impacts = {}
    for ticker in chain.get("sensitive_tickers", []):
        ticker_impacts[ticker] = {
            "exposure_tier": "HIGH" if ticker in ["HDFCBANK", "TSM", "NVDA", "TATAMOTORS"] else "MODERATE",
            "sensitivity_beta": 1.2 if ticker in ["HDFCBANK", "NVDA"] else 0.85,
            "expected_transmission": f"{'+' if normalized_intensity < 0 else '-'}{round(abs(normalized_intensity) * 4.2, 1)}% Margin Volatility",
            "status": "Vulnerable to Pass-Through" if normalized_intensity > 0 else "Liquidity & Supply Relieved"
        }

    return {
        "shock_id": chain["id"],
        "shock_title": chain["title"],
        "catalyst_entity": chain["catalyst_entity"],
        "applied_intensity_pct": round(intensity_pct, 1),
        "base_probability": round(base_prob, 3),
        "simulated_ripple_probability": round(net_terminal_prob, 3),
        "dampener_absorption_pct": int(effective_dampener * 100),
        "time_horizon": horizon_override or chain["time_horizon"],
        "transmission_channel": chain["transmission_channel"],
        "terminal_outcome": chain["terminal_outcome"],
        "simulated_steps": steps_output,
        "ticker_impacts": ticker_impacts,
        "dampeners": chain["dampeners"],
        "historical_precedent": chain["historical_precedent"]
    }
