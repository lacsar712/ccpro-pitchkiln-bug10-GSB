"""跨过道同牌脏数据的回归测试。

灶牌本应按过道唯一，但历史数据里存在两台不同主键的灶共用同一灶牌、
分属不同过道。看板的三条链路——过道号展示、按过道过滤、图例分相位
计数——都必须按灶台主键自身的 lane 归属，严禁用灶牌字符串做全局查找。
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import FireHearth, ResinLot

DUP_TAG = "同牌灶"


class CrossLaneDuplicateTagTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            "tester", "t@example.com", "pw"
        )
        # 甲过道同牌灶：保温；乙过道同牌灶：出胶。
        cls.hearth_a = FireHearth.objects.create(
            lane=1, tag=DUP_TAG, resinGrade="特级脂",
            phase=FireHearth.PHASE_HOLDING,
        )
        cls.hearth_b = FireHearth.objects.create(
            lane=2, tag=DUP_TAG, resinGrade="特级脂",
            phase=FireHearth.PHASE_DRAWING,
        )
        cls.hearth_other = FireHearth.objects.create(
            lane=2, tag="坑火-西二", resinGrade="二级脂",
            phase=FireHearth.PHASE_COLD,
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_board_groups_each_hearth_by_own_lane(self):
        """两台同牌灶各归各的过道，瓦片过道号不被规范灶顶替。"""
        resp = self.client.get(reverse("home"))
        lanes = dict(resp.context["lanes"])
        self.assertEqual(
            [h.pk for h in lanes[1]], [self.hearth_a.pk]
        )
        self.assertEqual(
            [h.pk for h in lanes[2]],
            [self.hearth_b.pk, self.hearth_other.pk],
        )
        # 同牌两台灶都必须出现，不能被按 tag 去重。
        tile_pks = {h.pk for row in lanes.values() for h in row}
        self.assertEqual(
            tile_pks,
            {self.hearth_a.pk, self.hearth_b.pk, self.hearth_other.pk},
        )

    def test_board_renders_each_tag_tile_in_its_own_lane_row(self):
        resp = self.client.get(reverse("home"))
        self.assertContains(resp, '<div class="lane-label">过道 1</div>', count=1)
        self.assertContains(resp, '<div class="lane-label">过道 2</div>', count=1)
        # 同牌灶两张瓦片都在，分属两行。
        self.assertContains(resp, f"hx-get=\"/hearth/{self.hearth_a.pk}/drawer/\"")
        self.assertContains(resp, f"hx-get=\"/hearth/{self.hearth_b.pk}/drawer/\"")

    def test_lane_filter_keeps_only_that_lane_pks(self):
        """过滤过道 1 只应留下过道 1 主键的灶，不得串入过道 2 同牌灶。"""
        resp = self.client.get(reverse("home"), {"lane": "2"})
        lanes = dict(resp.context["lanes"])
        self.assertEqual(set(lanes), {2})
        self.assertEqual(
            [h.pk for h in lanes[2]],
            [self.hearth_b.pk, self.hearth_other.pk],
        )
        self.assertEqual(resp.context["filter_lane"], "2")

        resp1 = self.client.get(reverse("home"), {"lane": "1"})
        lanes1 = dict(resp1.context["lanes"])
        self.assertEqual(set(lanes1), {1})
        self.assertEqual([h.pk for h in lanes1[1]], [self.hearth_a.pk])

    def test_grid_partial_lane_filter_no_cross_lane_tiles(self):
        resp = self.client.get(reverse("floor_grid"), {"lane": "1"})
        self.assertContains(resp, f"/hearth/{self.hearth_a.pk}/drawer/")
        self.assertNotContains(resp, f"/hearth/{self.hearth_b.pk}/drawer/")
        self.assertNotContains(resp, "过道 2")

    def test_legend_counts_each_hearth_independently(self):
        """图例逐灶台计数：同牌两灶的不同相位各计一次。"""
        resp = self.client.get(reverse("home"))
        counts = {key: count for key, _label, count in resp.context["phase_legend"]}
        self.assertEqual(counts[FireHearth.PHASE_HOLDING], 1)
        self.assertEqual(counts[FireHearth.PHASE_DRAWING], 1)
        self.assertEqual(counts[FireHearth.PHASE_COLD], 1)
        self.assertEqual(counts[FireHearth.PHASE_CHARGING], 0)
        self.assertEqual(sum(counts.values()), 3)

    def test_legend_scoped_to_filtered_lane(self):
        """图例与过滤对齐：过道 1 看板只数过道 1 的灶。"""
        resp = self.client.get(reverse("home"), {"lane": "1"})
        counts = {key: count for key, _label, count in resp.context["phase_legend"]}
        self.assertEqual(counts[FireHearth.PHASE_HOLDING], 1)
        self.assertEqual(counts[FireHearth.PHASE_DRAWING], 0)
        self.assertEqual(counts[FireHearth.PHASE_COLD], 0)

    def test_drawer_shows_the_pk_hearth_not_canonical_tag_hearth(self):
        """打开乙过道同牌灶的抽屉，必须显示乙过道这台主键灶本身。"""
        resp = self.client.get(
            reverse("hearth_drawer", args=[self.hearth_b.pk]),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.context["hearth"].pk, self.hearth_b.pk)
        self.assertEqual(resp.context["hearth"].lane, 2)
        self.assertContains(resp, "过道 2")
        self.assertContains(resp, "出胶")

    def test_drawer_open_run_belongs_to_the_pk_hearth(self):
        """抽屉里的进行中值守必须来自主键灶自身，不按灶牌串台。"""
        lot = ResinLot.objects.create(
            lotCode="脂-抽屉-001",
            originPlace="测试沟",
            arrivalKg=Decimal("10.00"),
            receivedAt=timezone.now() - timezone.timedelta(days=1),
        )
        run_b = self.hearth_b.runs.create(
            resinLot=lot,
            openedAt="2026-09-01T08:00:00Z",
            targetSoftPointC=Decimal("88.00"),
        )
        resp = self.client.get(
            reverse("hearth_drawer", args=[self.hearth_b.pk]),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.context["open_run"].pk, run_b.pk)
        self.assertEqual(resp.context["open_run"].hearth_id, self.hearth_b.pk)

    def test_home_with_drawer_query_param_uses_pk(self):
        resp = self.client.get(reverse("home"), {"hearth": str(self.hearth_b.pk)})
        self.assertTrue(resp.context["drawer_open"])
        self.assertEqual(resp.context["hearth"].pk, self.hearth_b.pk)
        self.assertEqual(resp.context["hearth"].lane, 2)
