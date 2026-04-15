# Conditional Recall: Why Sensitive Data Sharing Needs TEE + LLM

## The Problem

You have a database with sensitive data. A researcher, analyst, or partner needs to query it. Today you have two options:

1. **Give them access.** They see everything. You hope they don't misuse it.
2. **Don't give them access.** They see nothing. You lose the value of sharing.

Column-level RBAC (Snowflake, Immuta, BigQuery) gives you a third option: they see *some* columns. But the rules are static — written before any query runs, without seeing the actual data or understanding what the query is trying to do. You pick a fixed point on the privacy-quality frontier and hope it's right for every future query.

**The result:** you either over-share (privacy risk) or over-restrict (quality loss). There's no way to say "show salary distributions for compensation benchmarks but redact them for individual lookups" because the access rule can't understand the query.

## The Insight: Conditional Recall

What if the access decision could be made *per query*, by something that:

- **Sees the raw data** — understands what's actually sensitive in context, not just what column names suggest
- **Understands the query** — distinguishes aggregate analytics from individual record lookups
- **Makes a judgment** — maximizes information value while minimizing privacy leakage
- **Provably forgets everything** — the raw data never leaves, only the filtered result does

This is **conditional recall**: use the knowledge to make the decision, then provably forget the knowledge. The decision leaves. The data doesn't.

Traditional systems can't do this. If a system sees the data, it *keeps* the data — in logs, in memory, in caches, in the operator's access. You can't "use knowledge before you pay for knowledge" because there's no mechanism to guarantee forgetting.

## Why TEE + LLM

**Trusted Execution Environments** provide the forgetting guarantee. A TEE (like Intel SGX, AMD SEV, or Phala's Confidential VMs) runs code in an encrypted enclave. The operator can't see what's inside. When the computation ends, the enclave is destroyed. Remote attestation proves this cryptographically — anyone can verify that the code ran in a genuine TEE and that only the declared output left.

**Large Language Models** provide the judgment. An LLM can read a database schema, inspect the actual data, understand a natural-language query, and make a nuanced access decision — "this aggregation is safe because the groups are large enough, but suppress the results for departments with fewer than 10 people." Static RBAC rules can't express this. An LLM can.

Put them together:

```
Query arrives
    → LLM scope agent runs inside TEE
    → Agent reads raw data (to understand what's sensitive)
    → Agent reads the query (to understand intent)
    → Agent makes a per-query access decision
    → Only the filtered result leaves the TEE
    → TEE attestation proves the raw data was destroyed
```

The scope agent is the most powerful entity in the system — it sees everything — and that's what makes it safe. It can look at the actual data to decide if an aggregation leaks individuals. It can simulate the query agent to predict what it'll ask for. It can check group sizes for k-anonymity. And none of this leaks, because the TEE guarantees it.

## The Privacy-Quality Frontier

Traditional access control picks a single point: "analysts see columns A, B, C." That's a static tradeoff between privacy (share less) and quality (share more).

Conditional recall turns this into a **searchable optimization problem**. For each query, the scope agent finds the best point on the privacy-quality Pareto frontier:

| Query | Static RBAC | Conditional Recall |
|-------|------------|-------------------|
| "Average salary by department" | Block (salary column is restricted) | Allow (aggregate over large groups is safe) |
| "List all salaries" | Block | Block (individual records leak) |
| "Salary distribution for Engineering" | Block | Allow if group > 10, suppress if smaller |
| "What's Alice's salary?" | Block | Block (individual lookup) |

Same data. Same column. Different decisions — because the agent understands the query.

## How It Works in Practice

```bash
# Data owner describes their position on the frontier
hivemind scope "Share patient outcomes for medical research.
  Allow aggregate statistics and survival curves.
  Never expose individual patient records or names.
  Suppress groups smaller than 10."

# Researcher queries through the scope
hivemind query "What's the 5-year survival rate for stage 3 breast cancer
  patients who received immunotherapy?"

# → Returns: aggregated survival statistics
# → Raw patient data never left the TEE
```

The data owner writes English. The scope agent translates it into per-query access decisions. The TEE proves the raw data was forgotten.

## What This Is Not

- **Not differential privacy.** The scope agent is an intelligent mediator, not a formal privacy mechanism. It makes best-effort judgments, not mathematical guarantees.
- **Not a replacement for encryption.** The TEE protects data in use. You still need encryption at rest and in transit.
- **Not "just an LLM."** Without the TEE, conditional recall is just "trusting the platform operator." The TEE attestation is what makes the forgetting guarantee cryptographic rather than contractual.

## What This Enables

- **Data marketplaces** where sellers share sensitive data without losing control
- **Cross-organization analytics** where hospitals share patient outcomes without exposing individual records
- **Internal analytics** where HR shares workforce data for planning without exposing individual compensation
- **Regulatory compliance** where auditors query financial data without bulk export

The common thread: the data has value when queried, but sharing it creates risk. Conditional recall lets you capture the value while the TEE eliminates the risk.

---

*Hivemind implements conditional recall using Phala Network's Confidential VMs for the TEE, Claude for the scope agent, and a compile-time + runtime hybrid for performance. The scope agent prompt, adversarial evaluation harness, and CLI are open source.*
