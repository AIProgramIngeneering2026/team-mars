
import pytest

from mars.errors import ProcessingError
from mars.tools import BriefSummaryTool, TimelineDigestTool


@pytest.mark.parametrize(
    "tool_class",
    [BriefSummaryTool, TimelineDigestTool],
)
@pytest.mark.parametrize(
    "transcript, expected_field",
    [
        ([], "transcript"),
        ([{"startMs": -1, "text": "Текст"}],
         "transcript[0].startMs"),
        ([{"startMs": 0, "text": "   "}],
         "transcript[0].text"),
        ([{"startMs": 0, "text": "Текст"}, "bad segment"],
         "transcript[1]"),
        (
            [
                {"startMs": 100, "text": "Первый"},
                {"startMs": 50, "text": "Второй"},
            ],
            "transcript[1].startMs",
        ),
    ],
)
def test_invalid_transcript_reports_field(
    tool_class, transcript, expected_field
):
    tool = tool_class()

    with pytest.raises(ProcessingError) as caught:
        tool.run(transcript)

    assert caught.value.code == "INVALID_TRANSCRIPT"
    assert caught.value.field == expected_field
