from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.certificate import CertificateParser
from app.cli import compare_records
from app.jsonio import export_drafts, import_drafts


TEXT = """第 1 页 (共 1 页)
证书号第7668232号
发 明 专 利 证 书
发明名称：一种海上风机基础、拖航及安装方法
专利权人：华电科工股份有限公司
地址：100071 北京市丰台区
发 明 人：张健翔;张星波
赵玉琢
专 利 号：ZL 2020 1 0430096.0
授权公告号：CN 111472379 B
专利申请日：2020年05月20日
授权公告日：2025年01月14日
申请日时申请人：华电重工股份有限公司
申请日时发明人：张健翔;张星波
赵玉琢
国家知识产权局依照中华人民共和国专利法进行审查
"""


class CertificateParserTests(unittest.TestCase):
    def test_parses_text_layer_certificate_and_reconstructs_wrapped_list(self) -> None:
        draft = CertificateParser().parse_text(TEXT, "sample.pdf")

        self.assertEqual(draft.patent_type, "invention")
        self.assertEqual(draft.patent_no, "ZL202010430096.0")
        self.assertEqual(draft.title, "一种海上风机基础、拖航及安装方法")
        self.assertEqual(draft.application_date, "2020-05-20")
        self.assertEqual(draft.grant_publication_date, "2025-01-14")
        self.assertEqual(draft.current_patentees, ["华电科工股份有限公司"])
        self.assertEqual(draft.inventors, ["张健翔", "张星波", "赵玉琢"])
        self.assertEqual(draft.needs_review, [])
        self.assertIn("inventor_list_cross_line_reconstructed", draft.notes)

    def test_mismatched_required_field_is_reported(self) -> None:
        actual = {"patent_no": "ZL202010430096.0", "inventors": ["张健翔"]}
        expected = {"patent_no": "ZL202010430096.0", "inventors": ["张星波"]}

        differences = compare_records(actual, expected)

        self.assertEqual(len(differences), 1)
        self.assertIn("inventors", differences[0])

    def test_json_round_trip_keeps_manual_review_data(self) -> None:
        draft = CertificateParser().parse_text(TEXT, "sample.pdf")
        draft.add_review("inventors")
        with TemporaryDirectory() as directory:
            target = Path(directory) / "draft.json"
            export_drafts(target, [draft])
            restored = import_drafts(target)[0]

        self.assertEqual(restored.inventors, draft.inventors)
        self.assertIn("inventors", restored.needs_review)
        self.assertEqual(
            restored.field_evidence["inventors"].raw_value,
            draft.field_evidence["inventors"].raw_value,
        )


if __name__ == "__main__":
    unittest.main()
