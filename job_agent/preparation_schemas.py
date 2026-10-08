"""v6 专用结构：个人事实逐条引用；面试能力状态由宿主填写。"""

from pydantic import Field

from .schemas import Structured


class Revision(Structured):
    fact_id: str
    source_quote: str = Field(min_length=1)
    after: str = Field(min_length=1, max_length=1200)
    reason: str = Field(min_length=1, max_length=500)
    jd_ids: list[str] = Field(min_length=1)


class TailorOutput(Structured):
    revisions: list[Revision] = Field(max_length=8)
    project_order: list[str]
    skill_order: list[str]


class InterviewQuestion(Structured):
    topic_id: str
    question: str = Field(min_length=8, max_length=500)
    reference_answer: str = Field(min_length=30, max_length=1800)
    knowledge_points: list[str] = Field(min_length=1, max_length=6)
    follow_ups: list[str] = Field(max_length=4)
    learning_advice: str = Field(min_length=8, max_length=600)
    jd_ids: list[str] = Field(min_length=1)
    resume_ids: list[str]


class InterviewOutput(Structured):
    questions: list[InterviewQuestion] = Field(min_length=10, max_length=10)
