from html.parser import HTMLParser
from uuid import uuid4

from aris.interfaces.usage.report import history_report
from aris.modules.lyra.usage_history import usage_gpu_summary
from tests.test_recorded_usage import OWNER, measured
from tests.test_usage_history import reduce


def test_gpu_summary_keeps_physical_gpu_and_model_changes_without_double_counting_users():
    first = measured(start=0, end=20)
    second = measured(previous=first, start=20, end=40, model="renamed model")
    third = measured(previous=second, start=40, end=60, owner_id=uuid4())
    fourth = measured(previous=third, gpu_uuid="GPU-b", start=10, end=70)
    report = reduce((first, second, third, fourth))
    summary = usage_gpu_summary(report.rows)
    assert sum(gpu.recorded_microseconds for gpu in summary) == report.recorded_microseconds
    first_gpu = next(gpu for gpu in summary if gpu.gpu_uuid == "GPU-a")
    assert first_gpu.models == ("fixture GPU", "renamed model")
    assert first_gpu.owner_count == 2 and first_gpu.segment_count == 3
    assert first_gpu.recorded_microseconds == 60000000


class ReadReport(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = 0
        self.external_attributes = 0

    def handle_starttag(self, tag, attrs):
        self.scripts += tag == "script"
        self.external_attributes += sum(name in ("src", "href") for name, value in attrs)


def test_report_is_a_self_contained_readable_snapshot_and_escapes_all_untrusted_text():
    entry = measured(model='<img src="https://invalid.example/pixel">')
    report = reduce((entry,))
    raw = history_report(report, {str(OWNER): '<script>alert("fixture")</script>'}).decode()
    parser = ReadReport()
    parser.feed(raw)
    assert parser.scripts == parser.external_attributes == 0
    assert "&lt;script&gt;" in raw and "&lt;img src=" in raw
    assert "GPU 使用报告" in raw and "每日用量" in raw and "GPU 汇总" in raw
    assert report.fingerprint in raw and "0.008" in raw
    assert "不代表全部实际用量" in raw and "不用于配额扣减" in raw


def test_empty_report_describes_unrecorded_time_without_claiming_no_actual_usage():
    raw = history_report(reduce(()), {}).decode()
    assert "没有记录不代表 GPU 未被使用" in raw
    assert "这个时段没有已记录用量" in raw
    assert usage_gpu_summary(()) == ()
