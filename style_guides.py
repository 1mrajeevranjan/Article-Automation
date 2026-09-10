"""Writing style definitions injected into every agent prompt.

Grounded in published style guidance rather than invented conventions:
- ieee_paper: IEEE Editorial Style Manual for Authors + IEEE Access author guidelines
  (objective register, technical terms defined on first use, accessible to
  non-specialists, numbered [n] references in a separate closing section).
- blog_post: HubSpot voice & tone guidance for blog content
  (educational, conversational, optimistic; reader-focused, active voice).
"""

STYLES = {
    "ieee_paper": {
        "label": "IEEE Research Paper",
        "include_references": True,
        "voice": (
            "Write as an expert academic researcher preparing a peer-reviewed IEEE paper. "
            "Register is formal, impersonal, technical, and objective; third-person throughout. "
            "Define every technical term on first use, and keep the exposition accessible to a "
            "technically literate non-specialist rather than relying on unexplained jargon. "
            "Use dense conceptual layering and structured academic argumentation, with formal "
            "transitions (\"Furthermore\", \"In contrast\", \"Consequently\"). "
            "No conversational phrasing, no marketing tone, no storytelling, no rhetorical questions, "
            "no second-person address, no emojis, no bullet lists — paragraph-based academic prose only."
        ),
        "section_guidance": (
            "Sections should progress from theoretical/conceptual grounding toward system design, "
            "methodology, results/analysis, and applications/discussion, adapted to the specific topic. "
            "Explicitly address architectural implications, design trade-offs, scalability concerns, "
            "theoretical assumptions, and practical constraints. Avoid surface-level explanation."
        ),
        "abstract_guidance": (
            "A single unified paragraph in formal academic register, covering: the problem statement, "
            "the limitation of existing approaches, the proposed approach, the key contributions, and "
            "the broader implications."
        ),
    },
    "blog_post": {
        "label": "Blog Post",
        "include_references": False,
        "voice": (
            "Write as an experienced subject-matter expert writing for a professional blog audience. "
            "Tone is educational, conversational, and optimistic — knowledgeable without being stiff. "
            "Use active voice, second-person address (\"you\") where it helps the reader, and short "
            "paragraphs (2-4 sentences) that stay easy to scan. Lead with what matters to the reader "
            "and make every section practically useful. Explain jargon in plain language the first time "
            "it appears. Avoid academic stiffness, filler, and hype; no emojis, no bullet lists — "
            "flowing prose paragraphs only."
        ),
        "section_guidance": (
            "Sections should follow a reader's natural questions: what this is, why it matters, how it "
            "works in practice, what to watch out for, and what to do about it. Favour concrete examples "
            "and real-world framing over abstract theory."
        ),
        "abstract_guidance": (
            "A single engaging summary paragraph written as a blog intro / TL;DR: what the piece "
            "covers, why the reader should care, and what they will take away. Conversational but "
            "substantive — no academic framing."
        ),
    },
}

DEFAULT_STYLE = "ieee_paper"


def get_style(config: dict) -> dict:
    return STYLES.get(config.get("writing_style", DEFAULT_STYLE), STYLES[DEFAULT_STYLE])
