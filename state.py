from dataclasses import dataclass, field


@dataclass
class ArticleState:
    row_number: int
    title: str
    scope: str
    author: str = ""
    target_word_count: int = 0
    target_sections: int = 0

    outline: list[str] = field(default_factory=list)   # middle-section headings only
    section_drafts: dict[str, str] = field(default_factory=dict)  # heading -> text
    intro: str = ""
    conclusion: str = ""
    abstract: str = ""
    keywords: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)  # AI-generated IEEE-style citations — not verified real sources

    word_count_history: list[int] = field(default_factory=list)
    status: str = "Pending"
    notes: str = ""

    def body_word_count(self) -> int:
        parts = [self.intro, *self.section_drafts.values(), self.conclusion]
        return sum(len(p.split()) for p in parts if p)

    def middle_section_count(self) -> int:
        # target_sections includes Intro + Conclusion
        return max(self.target_sections - 2, 1)
