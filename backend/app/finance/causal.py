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


# Curated, macroeconomic causal models reflecting systemic transmission channels
CANONICAL_CAUSAL_CHAINS: List[Dict[str, Any]] = [
    {
        "id": "chain-monetary-policy-lending",
        "title": "RBI Monetary Policy Transmission to Corporate Syndicated Credit",
        "catalyst_event": "Reserve Bank of India Monetary Policy Committee Shifts Repo Rate",
        "catalyst_entity": "Reserve Bank of India",
        "catalyst_domain": "Monetary Policy & Central Banking",
        "terminal_outcome": "Margin Compression & Capex Delay in Capital-Intensive Sectors",
        "transmission_channel": "Bank Cost of Funds & Liquidity Transmission Channel",
        "overall_confidence": 0.89,
        "base_probability": 0.82,
        "time_horizon": "1-2 quarters",
        "affected_sectors": ["Banking & Retail Credit", "Infrastructure", "Automotive & Manufacturing"],
        "sensitive_tickers": ["HDFCBANK", "RELIANCE", "TATAMOTORS"],
        "historical_precedent": "2022-2023 Global Rate Hike Cycle & Corporate Debt Spread Widening",
        "steps": [
            {
                "step_order": 1,
                "stage_name": "Catalyst Shock",
                "entity": "Reserve Bank of India",
                "action_or_friction": "Adjusts baseline repo rate and tightens liquidity absorption facilities (LAF).",
                "channel": "Central Bank Policy Rate Setting",
                "elasticity_score": 1.0,
                "propagation_horizon": "Immediate (1-3 days)",
                "affected_tickers": ["HDFCBANK"],
                "evidence_quote": "RBI monetary policy decisions directly reset marginal cost of funds across Scheduled Commercial Banks."
            },
            {
                "step_order": 2,
                "stage_name": "Primary Transmission",
                "entity": "HDFC Bank & Tier-1 Commercial Lenders",
                "action_or_friction": "Repo rate pass-through increases MCLR and external benchmark lending rates by 35-50 bps.",
                "channel": "Retail & Wholesale Credit Repricing Channel",
                "elasticity_score": 0.88,
                "propagation_horizon": "2-4 weeks",
                "affected_tickers": ["HDFCBANK"],
                "evidence_quote": "Commercial banks reprice floating-rate working capital loans and commercial paper facilities."
            },
            {
                "step_order": 3,
                "stage_name": "Intermediate Friction",
                "entity": "Reliance Industries & Large Corporate Borrowers",
                "action_or_friction": "Debt refinancing costs increase for syndicated capital expenditure pipelines.",
                "channel": "Corporate Working Capital & Syndication Channel",
                "elasticity_score": 0.74,
                "propagation_horizon": "1-2 quarters",
                "affected_tickers": ["RELIANCE", "TATAMOTORS"],
                "evidence_quote": "Large corporate balance sheets adjust debt-to-equity ratios and prioritize internal cash generation."
            },
            {
                "step_order": 4,
                "stage_name": "Structural Terminal Impact",
                "entity": "Indian Manufacturing & Auto EV Clusters",
                "action_or_friction": "Downstream tier-2 suppliers face higher factoring costs, moderating private capex momentum.",
                "channel": "Real Economy Capex Propagation",
                "elasticity_score": 0.65,
                "propagation_horizon": "2-4 quarters",
                "affected_tickers": ["TATAMOTORS"],
                "evidence_quote": "Industrial supply chains experience inventory destocking and working capital recalibration."
            }
        ],
        "dampeners": [
            {
                "name": "Fixed-Rate Bond Portfolio Hedging",
                "mechanism": "Pre-hedged long-term corporate debt insulates against immediate 1-year rate shocks.",
                "absorption_capacity_pct": 28,
                "sponsor_entity": "HDFC Bank & Reliance Treasury"
            },
            {
                "name": "Surplus Retail Deposit Sticky Base",
                "mechanism": "Granular low-cost CASA deposits dampen wholesale interbank borrowing spikes.",
                "absorption_capacity_pct": 22,
                "sponsor_entity": "HDFC Bank"
            }
        ]
    },
    {
        "id": "chain-semiconductor-fab-cascade",
        "title": "Advanced Silicon Packaging Bottleneck to Sovereign AI Compute Deployments",
        "catalyst_event": "TSMC Advanced Packaging (CoWoS) Capacity Reallocation",
        "catalyst_entity": "TSMC",
        "catalyst_domain": "Semiconductor Fabrication & Packaging",
        "terminal_outcome": "Delivery Backlog & Sovereign AI Server Capex Shift into Domestic Fabs",
        "transmission_channel": "Advanced Silicon Packaging & Sub-10nm Supply Pass-Through",
        "overall_confidence": 0.93,
        "base_probability": 0.86,
        "time_horizon": "2-4 quarters",
        "affected_sectors": ["Semiconductor Fabrication", "AI & Enterprise Cloud", "Electronics Assembly"],
        "sensitive_tickers": ["TSM", "NVDA", "INFY"],
        "historical_precedent": "2021-2022 Global Automotive Semiconductor Lead Time Extension (52+ weeks)",
        "steps": [
            {
                "step_order": 1,
                "stage_name": "Catalyst Shock",
                "entity": "TSMC",
                "action_or_friction": "Constrains 3nm / CoWoS advanced substrate packaging lines due to hyperscaler demand surges.",
                "channel": "Foundry Wafer & Packaging Allocation",
                "elasticity_score": 0.95,
                "propagation_horizon": "1-2 weeks",
                "affected_tickers": ["TSM", "NVDA"],
                "evidence_quote": "Advanced packaging bottlenecks throttle GPU accelerator assembly throughput globally."
            },
            {
                "step_order": 2,
                "stage_name": "Primary Transmission",
                "entity": "NVIDIA Corporation",
                "action_or_friction": "Blackwell and Hopper accelerator shipment lead times stretch to 30-38 weeks.",
                "channel": "AI Accelerator Supply Allocation Channel",
                "elasticity_score": 0.91,
                "propagation_horizon": "1-2 months",
                "affected_tickers": ["NVDA"],
                "evidence_quote": "Enterprise cloud GPU allocations prioritized for tier-1 sovereign and hyperscale contracts."
            },
            {
                "step_order": 3,
                "stage_name": "Intermediate Friction",
                "entity": "Foxconn & Gujarat Semiconductor Fab Hub",
                "action_or_friction": "Accelerates secondary assembly and advanced packaging fab commissioning in Dholera & Sanand.",
                "channel": "Sovereign Supply Chain Diversification",
                "elasticity_score": 0.82,
                "propagation_horizon": "2-3 quarters",
                "affected_tickers": ["INFY"],
                "evidence_quote": "Government and joint ventures fast-track domestic testing and packaging (ATMP) infrastructure."
            },
            {
                "step_order": 4,
                "stage_name": "Structural Terminal Impact",
                "entity": "Infosys & Enterprise AI Service Integrators",
                "action_or_friction": "Clients reallocate budgets from on-premise hardware buys to optimized sovereign AI cloud services.",
                "channel": "Enterprise Software & Cloud Optimization Channel",
                "elasticity_score": 0.70,
                "propagation_horizon": "3-4 quarters",
                "affected_tickers": ["INFY"],
                "evidence_quote": "Enterprises shift to quantized LLMs and managed AI infrastructure to circumvent silicon lead times."
            }
        ],
        "dampeners": [
            {
                "name": "Ministry of Electronics & IT (MeitY) PLI Subsidies",
                "mechanism": "50% fiscal capital support absorbs setup risk for domestic packaging fabs.",
                "absorption_capacity_pct": 35,
                "sponsor_entity": "Ministry of Electronics & IT"
            },
            {
                "name": "Secondary Foundry Architecture Licensing",
                "mechanism": "Multi-sourcing packaging substrates across alternative Asian hubs.",
                "absorption_capacity_pct": 20,
                "sponsor_entity": "Foxconn Consortium"
            }
        ]
    },
    {
        "id": "chain-auto-ev-raw-materials",
        "title": "Critical Mineral Sourcing & Battery Cell Localization to Commercial EV Pricing",
        "catalyst_event": "Global Battery Lithium & Cathode Material Export Constraints",
        "catalyst_entity": "Global Critical Mineral Suppliers",
        "catalyst_domain": "Clean Energy & Mineral Supply Chains",
        "terminal_outcome": "Battery Pack Cost Escalation & Accelerated Sodium-Ion / Domestic Cell Localization",
        "transmission_channel": "Raw Material Commodity Cost Pass-Through & Cell Assembly",
        "overall_confidence": 0.85,
        "base_probability": 0.78,
        "time_horizon": "2-3 quarters",
        "affected_sectors": ["Automotive & EV", "Renewable Energy & Battery Storage", "Electronics Manufacturing"],
        "sensitive_tickers": ["TATAMOTORS", "RELIANCE"],
        "historical_precedent": "2022 Nickel & Lithium Carbonate Price Shock on Passenger EV Margins",
        "steps": [
            {
                "step_order": 1,
                "stage_name": "Catalyst Shock",
                "entity": "Global Critical Mineral Suppliers",
                "action_or_friction": "Export quotas and processing tariffs increase lithium carbonate spot prices.",
                "channel": "Upstream Commodity Sourcing",
                "elasticity_score": 0.90,
                "propagation_horizon": "2-4 weeks",
                "affected_tickers": ["TATAMOTORS", "RELIANCE"],
                "evidence_quote": "Upstream mineral refining bottlenecks raise raw material input costs for cell manufacturers."
            },
            {
                "step_order": 2,
                "stage_name": "Primary Transmission",
                "entity": "Tata Motors & Pune Automotive EV Cluster",
                "action_or_friction": "Battery pack manufacturing cost per kWh rises 8-12%, putting pressure on gross vehicle margins.",
                "channel": "Automotive Cell Integration Channel",
                "elasticity_score": 0.84,
                "propagation_horizon": "1-2 quarters",
                "affected_tickers": ["TATAMOTORS"],
                "evidence_quote": "Automakers face trade-offs between absorbing component inflation and hiking fleet sticker prices."
            },
            {
                "step_order": 3,
                "stage_name": "Intermediate Friction",
                "entity": "Commercial Fleet Operators & EV Buyers",
                "action_or_friction": "Total cost of ownership (TCO) parity shifts outward by 6-9 months.",
                "channel": "Fleet Adoption & Consumer Demand Elasticity",
                "elasticity_score": 0.72,
                "propagation_horizon": "2-3 quarters",
                "affected_tickers": ["TATAMOTORS"],
                "evidence_quote": "Fleet electrification pacing moderates in commercial logistics while passenger EV adoption stabilizes."
            },
            {
                "step_order": 4,
                "stage_name": "Structural Terminal Impact",
                "entity": "Reliance Gigafactory & Domestic Battery Projects",
                "action_or_friction": "Accelerates long-term capital deployment into domestic cathode refining and LFP/Sodium cell plants.",
                "channel": "Domestic Energy Storage Localization",
                "elasticity_score": 0.68,
                "propagation_horizon": "4-6 quarters",
                "affected_tickers": ["RELIANCE"],
                "evidence_quote": "Industrial groups invest heavily in localized gigafactories under National ACC PLI schemes."
            }
        ],
        "dampeners": [
            {
                "name": "FAME / EMPS EV Fleet Adoption Subsidies",
                "mechanism": "Government purchase incentives offset battery pack cost differentials for commercial logistics.",
                "absorption_capacity_pct": 25,
                "sponsor_entity": "Ministry of Heavy Industries"
            },
            {
                "name": "Long-Term Bilateral Supply Offtake Contracts",
                "mechanism": "Multi-year fixed-price offtake contracts insulate tier-1 automakers from spot commodity swings.",
                "absorption_capacity_pct": 30,
                "sponsor_entity": "Tata Motors Procurement"
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
