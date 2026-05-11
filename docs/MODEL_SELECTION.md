# Model Selection — OFM Telegram Bot

Research date: **2026-05-11**. Local execution target: **Ollama / GGUF**, **16 GB VRAM**, **>= 8K context**, **7B–22B params**, **Q4_K_M / Q5_K_M**. Single model must serve two roles:

1. **Classifier** — structured JSON output, strong instruction-following.
2. **Response generator** — NSFW-permissive roleplay as an OnlyFans creator persona, no refusals, in-character continuity.

Vanilla instruct models (Llama 3 Instruct, Qwen Instruct, Gemma, Phi) are excluded — they refuse explicit content. We need an abliterated / RP-finetuned model that retains enough instruction-following to produce reliable JSON.

---

## 1. Survey

| Model | Base | Size | Quant | Est. VRAM @ 8K | Strengths | Weaknesses | License | Ollama availability |
|---|---|---|---|---|---|---|---|---|
| **MN-12B-Mag-Mell-R1** | Mistral-Nemo-Base-2407 (ChatML) | 12B | Q4_K_M | ~8.5 GB (7.5 GB weights + ~1 GB KV) | Best-in-class 12B for RP prose; "minimal slop"; strong worldbuilding; uncensored by construction (base, not instruct); ChatML so JSON prompting is clean | ~1 year old (Nov 2024 merge); base-tuned so instruction-following is good but not Instruct-level; community sometimes notes it can drift in very long context | Apache-2.0 (Nemo base) | Yes — `hf.co/nchapman/mn-12b-mag-mell-r1:12b` (also `HammerAI/mn-mag-mell-r1`, 35K pulls) |
| **Cydonia-24B-v4.3** (TheDrummer) | Mistral-Small-3.1/3.2 24B | 24B | Q4_K_M | ~14 GB weights + ~1.5 GB KV = **~15.5 GB** (tight on 16 GB) | Latest in TheDrummer's flagship RP line (v4.3 = late 2025/early 2026); excellent NSFW + character consistency; 128K context; strong long-form coherence; Mistral v7 Tekken template | 24B is at the edge of 16 GB — leaves almost no headroom for OS/other apps; slower tok/s than 12B; technically 24B (just over the 22B band ceiling); some users prefer v4.1 prose | Mistral Research / non-commercial (Mistral Small 3) — **check before commercial use** | Yes — `moophlo/Cydonia-24B-v4.3-GGUF:Q4_K_M` (14 GB); also `Fermi/Cydonia-24B-v4.3-heretic-vision:Q4_K_M` ("heretic" = decensored, lower refusals) |
| **Cydonia-22B-v1.1** (TheDrummer) | Mistral-Small-2409 22B | 22B | Q4_K_M | ~13 GB weights + ~1.2 GB KV = ~14.2 GB | Fits 16 GB comfortably; well-loved in the RP community; Metharme/Mistral/Alpaca templates all work | Older (mid-2024); newer Cydonia 24B v4.x supersedes it for quality; v1.1 is the only version officially on Ollama under `jean-luc/cydonia` | Mistral Research | Yes — `jean-luc/cydonia:22b-v1.1-q4_K_M` |
| **Magnum-v4-22B** (anthracite-org) | Mistral-Small 22B | 22B | Q4_K_M | ~13 GB + KV = ~14 GB | "Claude-style" prose; strong NSFW; popular in SillyTavern community | "Strict roleplay only — do not expect general inference skills" (per maintainer notes) → **bad fit for the JSON-classifier role**; 1 year old | Mistral Research | Yes — `fluffy/magnum-v4-22b:q4_K_M` |
| **Magnum-v4-12B** (Nemo-based) | Mistral-Nemo 12B | 12B | Q4_K_M | ~8 GB | Smaller, fast on 16 GB; same Magnum dataset quality | Same instruction-following caveat as the 22B; superseded by Mag-Mell for most RP tasks per community sentiment | Apache-2.0 | Yes — via `LESSTHANSUPER/MAGNUM_V4-Mistral_Small:12b_*` and `anthracite-org` HF mirrors |
| **L3.1-8B-Stheno-v3.4** (Sao10K) | Llama-3.1 8B | 8B | Q4_K_M | ~5 GB weights + KV = ~6 GB | Tiny VRAM footprint; classic 1-on-1 RP tune; very fast | Older (mid-2024, no v3.5+ for L3.1); 8B ceiling on prose quality; Llama-3 chat template can be finicky for strict JSON; some lingering Llama-3 refusal patterns on edge cases | Llama 3 Community | Yes — `fluffy/llama-3.1-8b-stheno-v3.4:q4_K_M` |
| **Lumimaid-v0.2-12B** (NeverSleep) | Mistral-Nemo 12B | 12B | Q4_K_M | ~8 GB | Polished RP tune, NSFW-friendly, large curated dataset | Mid-2024 release, no major refresh; community sentiment is mixed vs. Mag-Mell which generally wins head-to-head | CC-BY-NC-4.0 (non-commercial) | Yes via community mirrors (search `lumimaid` on ollama.com); no first-party tag |
| **EVA-Qwen2.5-14B-v0.2** | Qwen 2.5 14B | 14B | Q4_K_M | ~9 GB + KV = ~10 GB | Trained on ChatML RP data over Qwen 2.5 (which has very strong instruction-following → good JSON); does not refuse adult content per maintainer | Qwen base = stronger reasoning but slightly stiffer prose than Nemo-based RP tunes; v0.2 from late 2024, no v0.3 confirmed | Qwen License (permissive but with restrictions) | Yes — `type32/eva-qwen-2.5-14b` (low pulls — newer/less battle-tested on Ollama) |
| **Dan's PersonalityEngine v1.3.0** | Mistral-Small 24B | 24B | Q4_K_M | ~14.5 GB | "Genuine generalist with personality" — trained on RP + storywriting + general instruction data → best of both worlds for the dual-role use case in principle | 24B = same VRAM pressure as Cydonia 24B; less hype than Cydonia in NSFW-specific benchmarks; Ollama presence is via community mirrors | Mistral Research | Community mirrors only — manual import / `hf.co/...` pull |
| **Llama-3.3-Euryale-70B-v2.3** (Sao10K) | Llama-3.3 70B | 70B | Q2_K | ~26 GB even at Q2 | Top-tier RP quality; flagship of Sao10K's lineup | **Does not fit 16 GB** at any usable quantization (Q2 ~26 GB, Q4 ~40 GB). Excluded. | Llama 3 Community | N/A for this hardware |

VRAM estimates assume llama.cpp/Ollama with FP16 KV cache. Mistral-Nemo's wider vocab makes its KV slightly heavier than Llama-3 per token, but both stay under ~1.5 GB at 8K. Real-world overhead adds ~0.5–1 GB for runtime.

---

## 2. Primary recommendation

**`MN-12B-Mag-Mell-R1` (Mistral-Nemo 12B merge, ChatML)**

This is the best fit on 16 GB by a comfortable margin: Q4_K_M weights are ~7.5 GB, leaving 6–7 GB of headroom for 8K+ context, batch overhead, and other GPU tenants. It is the most consistently praised 12B RP model in the LocalLLaMA / SillyTavern communities, with a strong reputation for prose quality ("minimal slop"), persona stability, and worldbuilding. Because it is built on the **Nemo base** (not the Instruct model), it has no built-in safety refusals to defeat — it will write explicit content from a system prompt without resistance. Nemo's native function-calling / JSON capability survives the merge well enough for the classifier role when prompted firmly (e.g. `Respond ONLY with valid JSON matching this schema: ...`).

## 3. Fallback recommendation

**`Cydonia-24B-v4.3` (Mistral-Small-3.x 24B, TheDrummer)**

If Mag-Mell's prose proves too "small-model" for your persona quality bar, step up to Cydonia 24B v4.3 (or the `heretic` decensored variant). It is the highest-quality RP-tuned model that still fits 16 GB at Q4_K_M (~14 GB weights, ~15.5 GB total at 8K). Tradeoff: nearly zero VRAM headroom, slower tokens/sec, and Mistral Small 3 base carries a non-commercial research license — **review before any commercial deployment**.

## 4. Ollama pull command (primary)

```bash
ollama pull hf.co/nchapman/mn-12b-mag-mell-r1:12b
```

Alternate community mirror (more pulls, identical weights):

```bash
ollama pull HammerAI/mn-mag-mell-r1:latest
```

Fallback:

```bash
ollama pull moophlo/Cydonia-24B-v4.3-GGUF:Q4_K_M
```

## 5. Rationale

Mag-Mell-12B wins as primary because the 16 GB / 8K-context / dual-role constraint genuinely favors the 12B class: at Q4_K_M it leaves real headroom (~6 GB), runs fast enough for interactive Telegram latency, and the Nemo base gives both clean ChatML formatting (helpful for JSON) and zero refusal training (helpful for NSFW). Magnum-22B and Stheno-8B were rejected — Magnum is RP-only per its maintainer and degrades on structured tasks, and Stheno's 8B ceiling shows in long-form persona work. Cydonia-24B-v4.3 is the fallback because it is the newest (late 2025) flagship RP tune that still fits 16 GB at Q4, and TheDrummer's lineage has the most consistent NSFW-quality reputation of any open-weights line. The honest tradeoffs: (a) Mag-Mell is ~14 months old as of this writing — newer is not always better in the RP-merge world (it remains a top recommendation in 2026 community lists), but verify with a head-to-head before committing; (b) the same prompt may need separate system-prompt tuning per role since a single model is doing both classification and RP. Slovak is not a strength for any candidate — Nemo handles it acceptably, Qwen-based EVA may do better if Slovak quality matters.

---

### Release/version notes (recency check, 2026-05-11)

- **Mag-Mell-R1**: merge published Nov 2024, no R2 confirmed as of this writing. Still recommended in early-2026 community lists.
- **Cydonia v4.3**: Released late 2025 (TheDrummer); v4.1 was Sep 27, 2025. v4.3 is current flagship.
- **Stheno v3.4**: mid-2024; no v3.5 for Llama-3.1 confirmed.
- **Magnum v4**: late 2024; anthracite-org has not published a public v5 line at this size as of search date.
- **EVA-Qwen2.5 v0.2**: late 2024; community sentiment positive but Ollama footprint is small.

Sentiment uncertainty: there is real disagreement online about whether Mag-Mell or Cydonia gives "better" prose — most agree Cydonia is more capable but only fits at a much higher VRAM cost. For an OnlyFans persona where character consistency + NSFW willingness matter more than peak prose, either is defensible; Mag-Mell wins on the operational margins.

### Sources

- [Mag-Mell on Ollama (nchapman)](https://ollama.com/nchapman/mn-12b-mag-mell-r1)
- [Cydonia-24B-v4.3 GGUF on Ollama (moophlo)](https://ollama.com/moophlo/Cydonia-24B-v4.3-GGUF)
- [Cydonia-24B-v4.3-heretic-vision on Ollama](https://ollama.com/Fermi/Cydonia-24B-v4.3-heretic-vision)
- [Cydonia 22B v1.1 on Ollama (jean-luc)](https://ollama.com/jean-luc/cydonia)
- [Stheno v3.4 on Ollama (fluffy)](https://ollama.com/fluffy/llama-3.1-8b-stheno-v3.4)
- [Magnum-v4-22B on Ollama (fluffy)](https://ollama.com/fluffy/magnum-v4-22b)
- [EVA Qwen 2.5 14B on Ollama (type32)](https://ollama.com/type32/eva-qwen-2.5-14b)
- [TheDrummer/Cydonia-24B-v4.3 on Hugging Face](https://huggingface.co/TheDrummer/Cydonia-24B-v4.3)
- [EVA-Qwen2.5-14B-v0.2 on Hugging Face](https://huggingface.co/EVA-UNIT-01/EVA-Qwen2.5-14B-v0.2)
- [Mistral Nemo 12B on Ollama library](https://ollama.com/library/mistral-nemo)
- [Will It Run AI — Mistral VRAM guide 2026](https://willitrunai.com/blog/mistral-models-gpu-requirements)
- [LocalLLaMA / SillyTavernAI community model list — April 2026 gist](https://gist.github.com/swyxio/324fc884061bf20e97a2ecbe59bae34a)
- [Latent.Space — Top Local Models April 2026](https://www.latent.space/p/ainews-top-local-models-list-april)
- [Best LLMs for Roleplay 2026 (noviai)](https://www.noviai.ai/models-prompts/best-llm-for-roleplay/)
- [Llama 3.3 70B Euryale — Private LLM blog](https://privatellm.app/blog/llama-3-3-70b-euryale-v2-3-local-ai-role-play)
