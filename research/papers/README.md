# Three papers for the literature review

Picked for centrality to the actual argument in `research/literature_review.tex`,
not just general relevance to 3D scene graphs — each is the single most
load-bearing citation for one of the three literatures BIM-Graphs sits between.

## `hughes2022_hydra.pdf`
**Hydra: A Real-time Spatial Perception System for 3D Scene Graph Construction
and Optimization.** Hughes, Chang, Carlone. RSS 2022, arXiv:2201.13360.

The sensor-driven scene-graph architecture our own positioning argument
responds to directly (§ "Sensor-Driven 3D Scene Graphs" and the Design
Breakdown appendix's layered-architecture figure). Read this to understand
the skeleton (building→rooms→places→objects→mesh) that nearly every system
in the field inherits, and why every layer there is *estimated* — the thing
BIM-Graphs inverts.

## `rana2023_sayplan.pdf`
**SayPlan: Grounding Large Language Models using 3D Scene Graphs for
Scalable Robot Task Planning.** Rana, Haviland, Garg, Abou-Chakra, Reid,
Sünderhauf. CoRL 2023, arXiv:2307.06135.

The collapse/expand interface our QnA design adopts directly — an LLM plans
over a building-scale graph by exploring a small subgraph at a time rather
than ingesting the whole scene. This is the property that makes any of the
navigation/QnA work downstream of connectivity recovery actually tractable.

## `lilis2017_spaceboundary.pdf`
**Automatic generation of second-level space boundary topology from IFC
geometry inputs.** Lilis, Giannakis, Rovas. *Automation in Construction* 76
(2017), DOI 10.1016/j.autcon.2016.08.044. (Publisher copy is paywalled;
this is the authors' own self-archived manuscript, via ResearchGate/
osarch.org — legitimate open access, not a bypass.)

Explicitly the closest prior solution to the actual problem BIM-Graphs
solves: our own audit found **zero** `IfcRelSpaceBoundary` across 257 MB of
real IFC, and this paper generates exactly that topology from raw geometry
— for building-energy simulation, a decade before anyone asked the same
question for robotics. Described in our lit review as "under-cited in
robotics." Read this one closest; it's the paper our own method's core
mechanism is most directly answering.
