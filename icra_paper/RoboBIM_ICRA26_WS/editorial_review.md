# RoboBIM — Camera-Ready Editorial Pass for ICRA 2026 Workshop on Prior Maps

**Editor:** Senior Area Chair (acting)
**Manuscript:** *RoboBIM: Scaling Robot Semantic Foundations through Agentic BIM Priors*
**Target venue:** "Robots Meet Prior Maps: Leveraging Geometric and Semantic Priors for Real-World Robot Autonomy" — ICRA 2026 Workshop
**Source files reviewed:** `conference_101719.tex`, `outputv4.pdf`, `references.bib`

---

## PART 1 — Camera-Ready Rewrite

> The rewrite below preserves all factual content of the v4 draft but tightens prose, eliminates redundancy with the appendix, restores SLAM/robotics terminology, and explicitly frames every contribution against the workshop call (geometric and semantic priors for real-world autonomy). Section labels mirror the existing IEEE conference template; figure/table references are kept consistent with the existing `.tex` structure. Broken references in the v4 build (`Sec. ??`, `Table ??`, `[?]`) are flagged and resolved.

### Title
**RoboBIM: A Policy-Gated, Agent-Compiled IFC-to-Navigation Contract for Robot-Ready Indoor Priors**

*(The current title — "Scaling Robot Semantic Foundations through Agentic BIM Priors" — is rhetorically catchy but obscures the concrete artifact. The proposed title foregrounds the technical contribution (the navigation contract) and the safety property (policy-gated, agent-compiled), both of which are central to the workshop theme.)*

### Abstract

Building Information Models (BIM), encoded in the Industry Foundation Classes (IFC) schema, contain dense geometric and semantic priors for indoor autonomy, yet contemporary IFC-to-robot pipelines extract only their geometric envelope — walls, doors, and IfcSpace volumes — discarding the affordance, material, and topological-context information required for robot-ready planning. Conversely, monolithic large-language-model (LLM) ingestion of raw IFC offers no safety envelope: hallucinated topology, fabricated door clearances, or dropped storeys silently corrupt downstream navigation. We present **RoboBIM**, a staged, policy-gated agentic pipeline that compiles an IFC file into a *versioned navigation contract* — a three-layer graph $\mathcal{G}=(\mathcal{O},\mathcal{W},\mathcal{T})$ comprising per-storey occupancy grids ($\mathcal{O}$), a Voronoi-derived waypoint graph with clearance-annotated edges ($\mathcal{W}$), and a room-transition topology ($\mathcal{T}$), enriched with per-room type, hazard, material, traversability, and building-level navigation policies. A deterministic geometric backbone produces the structural baseline; a seven-stage tool-augmented LLM agent (Gemini 3.0 Flash) thereafter probes the IFC through sandboxed code execution, tunes extraction hyperparameters under a clamped parameter policy, infers semantics, and gates every structural edit through a *topology regression filter* that rolls back any candidate that reduces baseline connectivity. On four public IFC models spanning residential, mixed-use, and office layouts (14–64 rooms, 2–6 storeys), RoboBIM increases room-type coverage from 51/131 to 89/131 rooms, augments hazard, material, and traversability fields where the deterministic baseline yields only nulls, and preserves connected topology in all runs at a cost of $403\pm83$ s and $269\pm67$ k input tokens per building. The contract is exported to Nav2, Open-RMF, GraphML, and GeoJSON, and accompanied by a deterministic natural-language briefing for grounding on-board robot LLMs without runtime IFC access. **Workshop alignment:** the contribution directly addresses the call for *leveraging geometric and semantic priors for real-world autonomy* by demonstrating how a high-fidelity architectural prior, when compiled with provable safety guarantees, can replace minutes of online metric-semantic SLAM with a one-shot deterministic ingest.

### I. Introduction

Mobile robots deployed in structured indoor environments routinely reconstruct metric-semantic representations from on-board sensing — Kimera [1], Hydra [2], and 3D scene-graph pipelines [18]–[22] are exemplars — even when a high-fidelity digital twin of the deployment site exists as an IFC model authored at design time [3]–[5]. This duplication discards architectural priors that the robot would otherwise expend minutes re-discovering. The dual extreme is equally unsatisfactory: consuming IFC verbatim yields priors that are geometrically complete yet semantically inert. Room function when `IfcSpace.LongName` is blank, floor material when material assignments are attached to `IfcSlab` subassemblies rather than to spaces, and per-robot traversability — the fields that distinguish a *map* from a *robot-ready prior* — are either absent from the schema or distributed across heterogeneous `IfcPropertySet` instances that elude deterministic parsing.

**The IFC semantic gap.** The canonical IFC-to-graph pipeline (`IfcSpace` extraction, generalized Voronoi diagram (GVD) segmentation, door-waypoint injection, ROS 2 serialization) is well-established [6]–[10]. It reliably yields occupancy grids, a room-transition topology, and a waypoint graph, but its semantic ceiling is the schema itself. Hazard inference (`is this floor wet?`), material inference (`is this corridor carpeted?`), and traversability scoring (`can a 0.55 m wheelbase clear furniture-dense rooms?`) lie outside its expressive reach.

**Monolithic LLM consumption is unsafe.** A tempting alternative is to submit the raw IFC to a frontier LLM and request a robot briefing. Prior work demonstrates that LLMs can reason over BIM in principle — MCP4IFC [11], ifcMCP [12], modular MCP-BIM architectures [13] for design authoring; Text2BIM [14] for generative BIM; BIM2RDT [15] for agentic BIM-to-digital-twin alignment — but these systems are tolerant of ambiguity in ways that robot priors are not. A hallucinated corridor, a fabricated door width, or a silently dropped storey can corrupt downstream planning under failure modes that are difficult to detect at runtime. None of the above pipelines provide a mechanism that guarantees the LLM-augmented output never degrades the deterministic topological baseline.

**Contribution: staged agentic compilation with a regression gate.** We frame IFC-to-prior compilation as a two-layer problem. A *deterministic backbone* extracts the structural graph $\mathcal{G}_{\text{static}}=(\mathcal{O},\mathcal{W},\mathcal{T})$. A *staged agentic layer* of seven LLM stages thereafter enriches $\mathcal{G}_{\text{static}}$ with the interpretive fields the backbone cannot infer. The agentic layer operates under two declarative policies — `agent_structural_policy=none` (no topology mutation outside dedicated review stages) and `agent_parameter_policy=fixed` (no out-of-clamp extraction-parameter writes) — and every candidate enrichment passes a *topology regression gate* that rolls back any edit that reduces connected-transition count below the deterministic baseline. The resulting artifact is a versioned *navigation contract* bundling occupancy, waypoints, topology, semantics, per-field confidence, and full provenance.

Fig. 1 instantiates the full vision: an offline agentic IFC-to-prior compiler (this paper) feeding an online dual-agent navigation loop (deferred to future work and outlined in Sec. V). The remainder of this manuscript focuses on the offline compiler. Concretely, we contribute:

- **(C1)** A three-layer, robot-consumable navigation graph extracted top-down from IFC, with native exports to Nav2, Open-RMF, GraphML, and GeoJSON, and per-room semantic-field reservations populated by the agentic layer.
- **(C2)** A staged, tool-augmented LLM agent (Gemini 3.0 Flash, seven stages) that tunes backbone extraction hyperparameters within clamped ranges, infers room types, hazard flags, materials, and per-robot traversability, and emits a deterministic natural-language briefing intended for the system prompt of an on-board robot LLM.
- **(C3)** A *topology regression gate* that audits every agent-proposed structural edit against the deterministic baseline and rolls back any regression, providing the first explicit safety envelope for LLM-mediated IFC enrichment to our knowledge.
- **(C4)** An evaluation on four public IFC models showing that agentic enrichment lifts room-type coverage from 51/131 to 89/131 rooms (+74%), populates hazard, material, and traversability fields where the deterministic baseline reports nulls, and preserves connected topology across all runs.

### II. Related Work

**Deterministic IFC-to-robot priors.** Hamieh et al. [3], [16] extract IFC into weighted hypergraphs for cascaded A\*; Follini et al. [4] established the canonical IFC-to-costmap pipeline; subsequent work [5], [10], [17] automated waypoint extraction for construction robots. The IFC-graph literature [6]–[9] formalizes topological graph generation from IFC. All of these methods share a common ceiling: the semantics they expose are exactly those explicit in the IFC schema, and fields such as hazards, floor materials, and per-robot traversability are either null or populated only for fully standards-conforming building models. RoboBIM's deterministic backbone resides in this lineage; the contribution is the agentic layer that compiles over it.

**3D scene graphs and perception-based semantic priors.** SayPlan [18], EmbodiedRAG [19], ContextMatters [20], and Taskography [21] construct hierarchical 3D scene graphs from on-board sensing and apply LLMs for high-level task planning. Ray et al. [22] specify structured interfaces for scene-graph reasoning. These pipelines solve the *complementary* problem — semantic richness without an *a priori* map. RoboBIM solves the *dual* — semantic richness *from* an *a priori* map, with no perception required at compile time. Pix2G [23] bridges the two by pairing IFC with online scene-graph construction; RoboBIM targets the upstream stage of making the IFC itself semantically rich enough to seed such a bridge.

**LLM-BIM integration (design-centric).** MCP4IFC [11], ifcMCP [12], modular MCP-BIM architectures [13], and Text2BIM [14] confirm that LLMs can interact with IFC via tool-calling interfaces, but target architectural design or generative authoring. BIM2RDT [15] applies agentic reasoning to point-cloud-to-BIM alignment. Kim et al. [24] use LLMs for construction-robot task planning *downstream* of a BIM. None expose a policy-gated, safety-constrained pipeline for compiling IFC into a robot navigation prior; this is the niche RoboBIM occupies.

**LLMs for affordance- and capability-aware planning.** SayCan [25] grounds LLM plans in robot affordances; AutoGPT+P [26] performs affordance-based task planning; VAMOS [27] and TIC-VLA [28] introduce capability-modulated navigation; AINav [29] and BrainBody-LLM [30] close the planning loop with adaptive replanning; IndoorR2X [31] coordinates indoor robots through LLM-driven planning. All assume the semantic prior is *given*. RoboBIM's navigation contract is engineered as a drop-in input to such planners.

**Positioning.** Table I summarizes the landscape. The distinguishing capabilities of RoboBIM are (i) *staged* rather than monolithic LLM invocation with explicit per-stage role boundaries, (ii) an *explicit topology regression gate* that enforces the safety property typical IFC-LLM pipelines lack, and (iii) a *multi-consumer contract* output rather than a single bespoke serialization.

### III. Methodology

#### A. System overview

The RoboBIM compiler comprises a deterministic geometric backbone and a tool-augmented LLM agent wrapper (Fig. 3). The backbone consumes a single IFC file and produces the structural graph $\mathcal{G}_{\text{static}}=(\mathcal{O},\mathcal{W},\mathcal{T})$ — per-storey occupancy grids $\mathcal{O}$, a clearance-annotated waypoint graph $\mathcal{W}$, and a room-transition topology $\mathcal{T}$. The agent wrapper executes seven stages over the same backbone, probing the IFC through a sandboxed Python interpreter, tuning backbone hyperparameters within clamped ranges, and writing semantic fields $\Sigma$ — room types, hazards, materials, traversability scores, and building-level navigation policies — into the graph. The output $\mathcal{G}_{\text{agent}}=(\mathcal{O},\mathcal{W},\mathcal{T},\Sigma)$ is exported to Nav2, Open-RMF, GraphML, and GeoJSON, alongside a deterministic natural-language briefing intended for the system prompt of an on-board robot LLM.

#### B. Deterministic backbone

The backbone is a five-module Python pipeline: `ifc_loader` (storey, door, and `IfcSpace` extraction), `occupancy` (per-storey rasterization of walls and columns into a 2D grid at configurable resolution; 5 cm in our experiments), `segmentation` (Euclidean signed distance field, GVD skeleton extraction, door-aware connected-component room labelling, with a purely geometric fallback when no `IfcSpace` entities are present), `waypoints` (skeleton tracing into a sparse graph with door-crossing waypoints injected to align the graph with the building's access topology), and `topology` (room-transition graph construction with material attachment from property sets and a cross-storey consistency check that flags stair or elevator transitions whose endpoints fail to align between floors). The output $\mathcal{G}_{\text{static}}$ is produced without LLM intervention and serves as the safety baseline against which agentic enrichments are compared.

#### C. Agentic enrichment layer

The agent wrapper adds seven stages, each reading from and writing to a shared *Pipeline Context*:

- **Stage 0 — IFC exploration.** The agent receives a compact IFC summary (entity counts, storeys, doors, representative property sets) and a sandboxed `IfcOpenShell`-backed Python tool. It autonomously generates code snippets to interrogate property sets, relationship entities, and element assemblies. The runner enforces a hard timeout, blocks file writes, and disallows non-standard imports.
- **Stage 1 — Quality and parameter tuning.** Conditioned on the Stage 0 evidence, the agent emits a structured object containing a completeness score, a recommended extraction strategy, and five clamped numeric parameters (grid resolution, door-closing dilation radius, minimum room area, door search radius, include-openings flag). Write-back is governed by `agent_parameter_policy ∈ {apply, suggest, fixed}`; the default is `fixed`, yielding logged-only parameters.
- **Stage 2 — Room-type inference.** A regex-and-property-set prefilter resolves rooms with deterministic evidence (e.g., names matching `corridor|CX|WC`; rooms containing ≥2 sanitary terminals → `bathroom`). Remaining rooms on a given storey are submitted to the LLM in a single batched prompt to expose cross-room context. The motivating example: an unlabelled space with multiple door transitions, bordered by office-typed rooms, is reliably inferred as a corridor — a regularity that resists rule-based encoding but is straightforward for an in-context LLM. New labels require confidence ≥ 0.80; overrides of existing labels require ≥ 0.75.
- **Stage 2b — Segmentation review.** The agent inspects the room list and transitions and may propose merge or split directives. Merges with confidence ≥ 0.70 are applied at the topology layer (bounding-box union, transition reassignment, absorbed-room removal). Split directives are recorded only, since they require pixel-level label-map access not preserved post-extraction.
- **Stage 3 — Connectivity validation and repair.** The agent enumerates topology issues (isolated rooms, suspicious transitions) and emits a repair plan over `add_connection`, `remove_connection`, and `reclassify` directives. Directives with confidence ≥ 0.70 are applied; the *topology regression gate* then audits the resulting connected-transition count and rolls back any edit that reduces it below the deterministic baseline.
- **Stage 4 — Hazards, materials, traversability.** For each room, the agent writes hazard flags from a controlled vocabulary {wet, obstacle-dense, high-traffic, narrow-passage}, a floor-material string, a traversability score in $[0,1]$, an obstacle summary, and a prose note. Per-storey batching and the Stage 2 prefilter are reused.
- **Stage 5 — Post-extraction audit.** The agent reads the final $\mathcal{G}_{\text{agent}}$ and the full agent trace, returning an overall quality score, completeness score, gap list, and improvement suggestions. The audit is recorded in the Pipeline Context and not applied to the graph; it is the input to the natural-language briefing.

The briefing is a deterministic templating of $\mathcal{G}_{\text{agent}}$ that lists each room (function, hazards, material, traversability) and derives building-level policies — tightest door-width invariant, caution rooms, non-navigable storeys — designed to be pasted verbatim into the system prompt of an on-board robot LLM. Runtime LLM access to the IFC is therefore *not* required.

#### D. Output graph and ROS 2 integration

The output graph has three linked layers (Fig. 4). **Layer 1** is a per-storey occupancy grid at 5 cm resolution with $\{free, occupied, unknown\}$ cell labels. **Layer 2** is a per-storey GVD-derived waypoint graph in which every edge carries a clearance value $c \in \mathbb{R}_{\geq 0}$ that determines whether a robot of inflated radius $r$ can traverse it ($c \geq r$). **Layer 3** is the topological graph of rooms and transitions (doors, wall openings, stair and elevator landings). Cross-layer links bind every room to its waypoint cluster and every transition to its crossing waypoint, supporting hierarchical descent from a topological plan to a metric trajectory in standard hierarchical-planner schemes.

The deterministic baseline reserves the semantic fields (room-type label with confidence and source, hazard flag list, floor-material string, traversability score) but leaves them empty. The agentic layer populates them. Building-level policies (tightest door-width invariant, non-navigable storeys) attach to the graph rather than to any single room.

### IV. Experiments

#### A. Setup

We evaluate on four public IFC models — `4351`, `BasicHouse`, `digital_hub`, and `samuel_macalister_sample_house` — covering residential, mixed-use, and office layouts of 14–64 rooms across 2–6 storeys. `BasicHouse` carries no `IfcSpace` entities and therefore exercises the geometric fallback. Provenance, licenses, and raw element counts are reported in the appendix. We run both the deterministic backbone and the full agent pipeline (Gemini 3.0 Flash preview) on every building. Each reported number is a single-run measurement; we discuss the implications of the absence of variance estimation in Sec. V. Comparisons span three axes: the geometric graph, the populated semantic fields, and the wall-clock and token cost of the agent pass.

#### B. Quantitative results

Table II reports per-IFC results. The *Geometry* columns are identical for both pipelines because the agent does not mutate $(\mathcal{O},\mathcal{W},\mathcal{T})$ outside of confidence-gated, regression-audited Stage 2b/3 edits. The *Semantic enrichment* columns are written `static → agent`. We report enrichment along five axes — type, hazard, traversability, material, and policy — defined as in the v4 draft.

Aggregating across the corpus, room-type coverage rises from 51 of 131 rooms to 89 of 131 rooms (+74.5%); hazard, traversability, material, and policy fields move from uniformly null in the deterministic baseline to populated in the agentic output. On `BasicHouse`, where no `IfcSpace` entities exist, the agent labels all 7 navigable rooms from geometry and adjacency alone, including hazard inference for kitchen, bathroom, and laundry zones. On `digital_hub`, the agent closes the 38-of-64 gap left by the regex prefilter and reaches full coverage. **No agent-proposed edit reduced connected-transition count below the deterministic baseline on any of the four runs**, confirming that the topology regression gate held in this corpus.

Agent wall-clock time ranges 294–489 s/IFC (mean 403 s); input tokens 200–348 k; output tokens 24–41 k. Per-storey batching keeps the total LLM call count between 7 and 15 per run. Stage 0 and Stage 2 dominate the time budget; Stage 2b dominates on `BasicHouse` (geometric fallback); Stage 3 dominates on `digital_hub` (densest connectivity graph). Per-stage and per-IFC breakdowns appear in Fig. 6 and the appendix.

#### C. Qualitative analysis

Fig. 5 contrasts the static and agent outputs on Floor 0 of `BasicHouse`. The static pipeline emits uniformly grey rooms with geometric indices and a waypoint graph; the agent output colours rooms by inferred type (`living_room`, `bedroom`, `bathroom`, `laundry`, `storage`) and surfaces hazard codes (W = wet, O = obstacle-dense, T = high-traffic, N = narrow). Listing 1 reproduces a four-room excerpt of the generated briefing, enumerating type, hazards, material, traversability, and derived navigation policies (tightest door width 0.91 m, robot width limit 0.81 m, caution rooms, non-navigable storeys) — none of which the deterministic baseline populates.

#### D. Online query agent (motivation and outlook)

The exports and briefing can be served to an on-board LLM through the Model Context Protocol (MCP) [32]. When the local planner reports a blocked door or occluded corridor, an on-board agent can query alternatives that satisfy width, hazard, and traversability constraints, allowing the robot to reroute against the prior rather than re-explore the global state space. The building context can be delivered through context-window concatenation or retrieval-augmented generation (RAG). This online loop is the motivating downstream consumer of the contract and is sketched in Fig. 1; its empirical evaluation is deferred to future work, and the abstract has been corrected to reflect this scope.

### V. Discussion and Future Work

The accuracy ceiling of any IFC-derived prior is the IFC file itself: the as-designed state, not the as-built one. Closing the design–build gap is the principal direction this work opens. Three extensions are immediate.

**Perception fusion.** $\mathcal{G}_{\text{agent}}$ is precisely the ground-truth target that Hydra-class systems [2] attempt to reconstruct. A fusion layer can use the BIM prior as a stable reference and update it with online SLAM observations or VLM-emitted captions, yielding an *as-built* prior that preserves an authoritative starting point.

**Richer graph layers.** The output schema reserves slots for two further layers — a *containment* layer enumerating per-room objects and a *boundary* layer encoding per-surface materials. Both are recoverable from IFC relationship entities (`IfcRelContainedInSpatialStructure`, `IfcRelAssociatesMaterial`) that the current pipeline does not exhaust. Each additional layer enables additional planner behaviour — e.g., LiDAR-aware penalisation of glass curtain walls, or manipulator-aware navigation around fragile objects.

**Adversarial and large-scale evaluation.** The topology regression gate has not yet been stress-tested under hostile prompting. On the four buildings reported here, agent confidence remained below threshold on every risky proposed edit, but a targeted adversarial prompt suite — synthetic corridors, fabricated transitions, dropped storeys — would establish the gate's empirical robustness. A larger building corpus (the BIMServer test set contains > 30 redistributable models) would also test the per-storey-batching scaling claim.

### VI. Conclusion

We presented **RoboBIM**, an IFC-to-navigation-graph compiler that treats IFC processing as semantic enrichment over a deterministic geometric backbone. The backbone produces the structural graph $\mathcal{G}_{\text{static}}=(\mathcal{O},\mathcal{W},\mathcal{T})$; a seven-stage tool-augmented LLM agent thereafter writes room types, hazard flags, traversability scores, materials, and building-level navigation policies into the graph under a parameter policy and a topology regression gate that together prevent the agentic layer from degrading the deterministic baseline. On four public buildings the agent raises type coverage by 74.5%, populates hazard, material, traversability, and policy fields where the baseline is null, and preserves connected topology in every run. The natural next step is to close the as-designed/as-built gap through perception fusion, to exhaust the IFC relationship entities for richer graph layers, and to evaluate the online query agent that reuses the same tool interface — so that the robot can interrogate the prior continuously as its plan meets the real building. The contract framing — versioned, multi-consumer, provenance-bearing — directly answers the workshop call for *robot-ready geometric and semantic priors* that scale beyond bespoke per-deployment integrations.

---

## PART 2 — Editorial Review

### Unsubstantiated or imprecise claims

- **Abstract numerics conflict with the body.** The abstract claims "increases room classification coverage" without a quantitative figure, while Table II reveals an aggregate 51→89 jump (+74.5%). The headline number belongs in the abstract. Correspondingly, "produces traversability scores for all navigable rooms" is true, but trivially so: the agent is *required* to emit a score per room — coverage is a tautology, not a result. Reframe as *"the deterministic baseline emits no traversability annotations whereas the agent emits a $[0,1]$ score for all 131 navigable rooms."*
- **"Significant boost" is unsubstantiated.** Contribution C3 in the v4 draft asserts a "significant boost in semantic coverage," but no statistical test, no inter-annotator agreement, and no human-evaluated correctness study is reported. *Coverage* and *correctness* are conflated. The agent labels 7/7 rooms in `BasicHouse`, but the paper never demonstrates those labels are correct — only that they are non-null.
- **`n=1` per building.** The paper acknowledges single-run measurements but does not discuss the implications. Gemini 3 Flash is non-deterministic (temperature implicitly > 0). Without at least 3–5 seeds per building, claims about wall-clock cost (403 s mean), token count, and stage dominance are not reproducible. This is the single weakest point in the empirical case.
- **Confidence thresholds are arbitrary.** 0.70/0.75/0.80 thresholds appear in Stages 2/2b/3 with no derivation, no sensitivity analysis, and no discussion of why three different thresholds are warranted. A planner that depends on these gates needs to know how the artifact behaves under threshold sweep.
- **The "topology regression gate" guarantee is weaker than advertised.** The gate compares *connected-transition count*, a single scalar. A semantically harmful edit that preserves count (e.g., re-routing a corridor through a kitchen) would slip through. The paper sells the gate as a safety envelope but does not establish what class of regressions it actually catches. State the gate's invariant precisely (it is a *connectedness preservation* gate, not a topology-equivalence gate) and acknowledge its limits.
- **"Agent pass remained topologically safe in these experiments."** This is correct phrasing, but the contribution paragraph in Sec. I generalises it to a property of the system. Distinguish *audited safety property* (the gate) from *empirical safety observation* (zero rollbacks on $n=4$).
- **Abstract/body inconsistency on online agent.** The abstract proposes an online query agent; Sec. I explicitly defers it to future work; Sec. IV-D motivates it qualitatively without evaluation. The abstract should be aligned with the actual scope (offline compiler only).

### LLM/ROS 2 architectural gaps

- **The MCP bridge is named but not specified.** Fig. 1 (bottom) shows an "MCP-ROS Bridge" that "translates BIM commands to ROS 2 actions." No paragraph in the body specifies what those commands are, what action server contracts they map to (`NavigateToPose`, `FollowWaypoints`, lifecycle transitions?), or how `Nav2Goal` flows back into the planner's reasoning loop. Even though the loop is deferred, a half-page schematic of the action interface — even at the level of "tool name → ROS 2 action server" — would harden the architectural claim that the contract is drop-in for ROS 2.
- **`Nav2` export format is unspecified.** The paper repeatedly claims Nav2 export, but does not state whether this is `nav_msgs/OccupancyGrid` YAML pairs, `Nav2 Behavior Tree` configuration, the `keepout_filter` mask format, or all three. Each implies different downstream integration effort.
- **Online RAG integration is hand-waved.** Sec. IV-D mentions "context window or retrieval-augmented generation (RAG)" but does not specify the chunking strategy, the embedding model, or how the contract's structured fields (numeric traversability, enum hazards) are linearised for retrieval. For a workshop that explicitly targets *real-world autonomy*, this is the integration story the audience will press hardest.
- **Agent-failure semantics are undefined.** What happens when Stage 0 returns no findings, or when the sandboxed runner times out? The paper says runs are gated, but not whether degraded agent runs fall back to the deterministic backbone wholesale or only at the regressed stage.
- **Confidence calibration is asserted, not measured.** The pipeline gates on raw LLM-emitted confidence values, which are notoriously miscalibrated. There is no calibration plot (reliability diagram) or temperature scaling reported.

### Structural / narrative weaknesses

- **The Methodology section duplicates the Appendix.** Sec. III-C and Appendix A–G describe the seven stages twice, with near-identical prose. Camera-ready space is at a premium; collapse to a single, tighter description in the body and let the appendix carry only the stage-specific implementation details that genuinely differ (clamp ranges, prefilter regexes, vocabulary lists).
- **Fig. 2 (system overview) is redundant with Fig. 3 (static vs. agent).** Drop Fig. 2 or merge it into Fig. 3 to recover space.
- **Workshop alignment is implicit, not explicit.** The workshop call foregrounds *"leveraging geometric and semantic priors for real-world robot autonomy."* The current draft makes a strong implicit case but never says, in one sentence, "RoboBIM addresses the workshop's call by …". A single workshop-alignment paragraph in the introduction (and a parallel sentence in the conclusion) would harden positioning.
- **Broken cross-references in the build.** `Sec. ??` (Fig. 1 caption — `\label{sec:outlook}` is missing on Sec. V), `Table ??` (cited in Appendix G — referent likely the per-stage timing table that did not make the camera-ready cut), and `[?]` (DAAAM citation on line referencing "as-built priors" — no `daaam` entry in `references.bib`). These must be fixed before submission; a `latexmk -pdf` run will surface them.
- **Typo in abstract.** "conmpared" → "compared".
- **Tense and voice drift.** The methodology mixes present-tense and past-tense narration of the same pipeline (e.g., "the loader parses…" / "the agent received…"). Standardise on present tense for system description and past tense for experimental observations.
- **Contribution C3 conflates description and claim.** "A study on four public building models, and showing that agent pipelines offer a significant boost…" should split into (a) a methodological contribution (the study) and (b) an empirical claim with a quantitative anchor.
- **References are inconsistent.** Some entries in `references.bib` use `arXiv preprint arXiv:NNNN.NNNNN` strings while others use journal/conference forms, and at least three (refs [20], [28], [31]) carry 2026 arXiv IDs that the camera-ready reviewer will flag — verify these resolve before submission.

---

## PART 3 — Experimental Roadmap

The empirical case is the manuscript's weakest pillar. The two reviewer questions most likely to surface are: **(Q1)** *what does the LLM actually buy you over a strong deterministic baseline?* and **(Q2)** *are the agent-emitted labels correct, or merely populated?* The three studies below address those questions in priority order.

### Study 1 — Stage-wise ablation against a strengthened deterministic baseline (addresses Q1)

The current deterministic baseline is a regex + property-set prefilter. This is a weak strawman: any sufficiently tuned deterministic pipeline would close part of the gap. To isolate the LLM's contribution, run a 2-D ablation grid:

| Configuration | Stage 0 | Stage 1 | Stage 2 prefilter | Stage 2 LLM | Stage 2b | Stage 3 | Stage 4 |
|---|---|---|---|---|---|---|---|
| **B0** Deterministic (current baseline) | — | — | ✓ | — | — | — | — |
| **B1** Deterministic + WordNet/spaCy semantic prefilter | — | — | ✓+NLP | — | — | — | — |
| **B2** Deterministic + rule-based hazard ontology | — | — | ✓+NLP | — | — | — | rules |
| **B3** Monolithic LLM (raw IFC → JSON, single call) | — | — | — | full | — | — | full |
| **A1** RoboBIM Stage-2-only | ✓ | — | ✓ | ✓ | — | — | — |
| **A2** RoboBIM Stages 0–4 (no review/repair) | ✓ | ✓ | ✓ | ✓ | — | — | ✓ |
| **A3** RoboBIM full pipeline | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

For each configuration on each of the four buildings, report (i) coverage on each of the five enrichment axes, (ii) correctness against ground-truth labels (see Study 2), (iii) wall-clock time, and (iv) LLM call count. The expected finding is a knee-of-the-curve plot where A1 captures most of the coverage gain, A2 contributes correctness via prefilter, and A3 contributes safety via Stages 2b/3 — quantifying *which* part of the pipeline is load-bearing. **B3 is the critical strawman** that the paper's thesis (monolithic LLM is unsafe) rests on; the paper currently asserts this without measurement.

### Study 2 — Ground-truth room-type and hazard accuracy (addresses Q2)

The current evaluation reports *coverage* (rooms with non-null fields) but not *correctness*. Construct ground truth by:

1. Two annotators (one architect, one roboticist) independently label every room across the four buildings with type (closed vocabulary: 12 classes from {office, corridor, kitchen, bathroom, bedroom, …}), hazard flags (4 classes), and floor material (5 classes).
2. Adjudicate disagreements; report Cohen's $\kappa$ for inter-annotator agreement as a calibration of difficulty.
3. Score every pipeline configuration's outputs against this ground truth.

Report **per-axis F1 and per-class confusion matrices**, plus a **calibration analysis**: bin agent-emitted confidence into deciles and plot reliability diagrams (predicted confidence vs. empirical accuracy). This is also the natural place to report the threshold sensitivity that the current paper omits — sweep the 0.70/0.75/0.80 gates over $\{0.5, 0.6, 0.7, 0.8, 0.9\}$ and plot precision–coverage curves. **Recommended headline metric:** macro-F1 on room-type classification; **secondary:** Brier score for confidence calibration; **tertiary:** Expected Calibration Error (ECE).

### Study 3 — Online query agent: token efficiency, latency, and replanning success (addresses workshop alignment)

Even though the online loop is "deferred," a single-figure quantitative teaser elevates the paper's relevance to a workshop on *real-world autonomy* substantially. Construct a synthetic disturbance suite (10 scenarios per building: blocked door, occluded corridor, lift offline, transient obstacle) and compare three on-board configurations:

- **C-baseline** — robot replans without prior, falls back to frontier exploration;
- **C-RAG** — robot queries the contract via RAG over the structured fields;
- **C-context** — robot is initialised with the natural-language briefing in its system prompt and replans from context.

Report:

| Metric | What it measures | Why it matters |
|---|---|---|
| **Replan success rate** | fraction of disturbances resolved without re-exploration | end-to-end utility of the prior |
| **Time-to-first-action** (s) | latency from disturbance detection to next action issuance | real-world responsiveness |
| **Tokens per query** | input + output tokens issued during replan | deployment-cost proxy for on-board LLM |
| **Plan optimality** (path length / reference) | quality of the replanned route | preventing the prior from short-circuiting good planning |
| **Topological accuracy** | fraction of replanned routes that traverse only nodes/edges that exist in the post-disturbance graph | catches hallucinated routes |

A small-scale (10–20 episode) study even on a single building (`digital_hub` is the natural choice given its connectivity density) would substantially strengthen the workshop submission.

### Optional Study 4 — Adversarial probe of the topology regression gate

The gate is a central safety claim. Construct adversarial prompts that solicit (i) bad merges (suggest merging unrelated rooms across a wall), (ii) fabricated connections (suggest a non-existent corridor), (iii) dropped storeys (suggest a storey is non-navigable when it is). Measure rollback rate. A 100% rollback rate at adversarial prompts would substantially harden the safety claim and is feasible within the workshop timeline.

### Summary of recommended headline metrics for the camera-ready

1. **Room-type macro-F1** against human ground truth (replaces "coverage").
2. **Brier score / ECE** on agent confidence (currently uncalibrated).
3. **Tokens / latency** per query on the online loop (currently absent).
4. **Topological-accuracy preservation rate** under adversarial Stage-3 prompts (validates the gate).
5. **Wall-clock $\pm$ s.d.** over $\geq 3$ seeds per building (replaces single-run measurements).

---

## Build-time fixes (apply before the camera-ready run)

1. Remove the duplicate (commented-out) abstract in `conference_101719.tex` lines 65–68 and replace with the rewritten abstract in Part 1 above.
2. Fix `\label{sec:outlook}` on Sec. V to resolve `Sec.??` in Fig. 1 caption.
3. Add a `\label{tab:per-stage-timing}` (or remove the citation) to silence `Table??` in Appendix G.
4. Add a `daaam` entry to `references.bib` (DAAAM-style captions citation) or remove the `\cite{?}` in Sec. V.
5. Resolve the abstract typo `conmpared → compared`.
6. Verify all `arXiv:2026.*` and `arXiv:2602.*`/`arXiv:2603.*` IDs (refs [20], [28], [31]) actually resolve; if pre-print IDs are speculative, replace with venue-resolved citations.
7. Standardise tense (present for system description, past for experiments).
