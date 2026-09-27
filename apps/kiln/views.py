from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Prefetch
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .forms import OpenCookRunForm, PhaseChangeForm, ResinLotForm, SoftPointProbeForm
from .models import CookRun, FireHearth, ResinLot
from .services.floor_rules import change_hearth_phase


def _wants_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _hearths_for_board():
    return FireHearth.objects.prefetch_related(
        Prefetch(
            "runs",
            queryset=CookRun.objects.filter(closedAt__isnull=True)
            .select_related("resinLot")
            .prefetch_related("probes"),
            to_attr="open_runs_cache",
        )
    ).order_by("lane", "tag")


def _resolve_by_tag(tag: str):
    return FireHearth.objects.filter(tag=tag).order_by("id").first()


def _board_context(lane_filter=None):
    hearths = list(_hearths_for_board())
    if lane_filter not in (None, ""):
        try:
            lane_num = int(lane_filter)
        except (TypeError, ValueError):
            lane_num = None
        if lane_num is not None:
            tags_on_lane = {h.tag for h in hearths if h.lane == lane_num}
            hearths = [h for h in hearths if h.tag in tags_on_lane]

    by_tag = {}
    for h in hearths:
        by_tag[h.tag] = _resolve_by_tag(h.tag) or h

    lanes = {}
    for tag, h in by_tag.items():
        display = _resolve_by_tag(tag) or h
        lanes.setdefault(display.lane, []).append(display)

    legend_by_tag = {}
    for h in FireHearth.objects.all():
        canon = _resolve_by_tag(h.tag) or h
        legend_by_tag[h.tag] = canon.phase

    phase_legend = [
        (key, label, sum(1 for ph in legend_by_tag.values() if ph == key))
        for key, label in FireHearth.PHASE_CHOICES
    ]
    return {
        "hearths": list(by_tag.values()),
        "lanes": sorted(lanes.items()),
        "phase_legend": phase_legend,
        "filter_lane": lane_filter or "",
    }


def _drawer_context(hearth):
    canon = _resolve_by_tag(hearth.tag) or hearth
    open_run = canon.open_run()
    probes = []
    if open_run:
        probes = list(open_run.probes.order_by("-sampledAt", "-id"))
    return {
        "hearth": canon,
        "open_run": open_run,
        "probes": probes,
        "phase_form": PhaseChangeForm(hearth=canon),
        "probe_form": SoftPointProbeForm() if open_run else None,
        "open_run_form": OpenCookRunForm(hearth=canon) if open_run is None else None,
        "requested_lane": hearth.lane,
    }


@login_required
def home(request):
    lane = request.GET.get("lane", "").strip()
    ctx = _board_context(lane_filter=lane or None)
    drawer_pk = request.GET.get("hearth")
    if drawer_pk:
        try:
            hearth = FireHearth.objects.get(pk=drawer_pk)
            ctx.update(_drawer_context(hearth))
            ctx["drawer_open"] = True
        except (FireHearth.DoesNotExist, ValueError):
            ctx["drawer_open"] = False
    else:
        ctx["drawer_open"] = False
    return render(request, "floor/board.html", ctx)


@login_required
def floor_grid_partial(request):
    lane = request.GET.get("lane", "").strip()
    html = render_to_string(
        "floor/_grid.html", _board_context(lane_filter=lane or None), request=request
    )
    return HttpResponse(html)


@login_required
def hearth_drawer(request, pk):
    hearth = get_object_or_404(FireHearth, pk=pk)
    ctx = _drawer_context(hearth)
    if _wants_htmx(request):
        return render(request, "floor/_drawer.html", ctx)
    return redirect(f"/?hearth={pk}")


@login_required
@require_POST
def change_phase(request, pk):
    hearth = get_object_or_404(FireHearth, pk=pk)
    form = PhaseChangeForm(request.POST, hearth=hearth)
    if form.is_valid():
        try:
            change_hearth_phase(hearth, form.cleaned_data["phase"])
            messages.success(request, f"灶牌 {hearth.tag} 相位已更新")
        except ValidationError as exc:
            msg = (
                exc.message_dict.get("phase") if hasattr(exc, "message_dict") else None
            )
            messages.error(request, msg[0] if msg else str(exc))
    else:
        err = form.errors.get("phase")
        messages.error(request, err[0] if err else "相位切换失败")

    if _wants_htmx(request):
        hearth.refresh_from_db()
        resp = render(request, "floor/_drawer.html", _drawer_context(hearth))
        resp["HX-Trigger"] = "floor-refresh"
        return resp
    return redirect(f"/?hearth={pk}")


@login_required
@require_POST
def add_probe(request, pk):
    hearth = get_object_or_404(FireHearth, pk=pk)
    open_run = hearth.open_run()
    if open_run is None:
        messages.error(request, "没有进行中的值守，无法登记探针")
        return redirect(f"/?hearth={pk}")

    form = SoftPointProbeForm(request.POST)
    if form.is_valid():
        probe = form.save(commit=False)
        probe.run = open_run
        probe.save()
        messages.success(request, f"已登记探针 {probe.softPointC}℃")
    else:
        messages.error(request, "探针登记失败，请检查输入")

    if _wants_htmx(request):
        resp = render(request, "floor/_drawer.html", _drawer_context(hearth))
        resp["HX-Trigger"] = "floor-refresh"
        return resp
    return redirect(f"/?hearth={pk}")


@login_required
@require_POST
def open_run(request, pk):
    hearth = get_object_or_404(FireHearth, pk=pk)
    form = OpenCookRunForm(request.POST, hearth=hearth)
    if form.is_valid():
        run = form.save(commit=False)
        run.hearth = hearth
        run.save()
        if hearth.phase == FireHearth.PHASE_COLD:
            hearth.phase = FireHearth.PHASE_CHARGING
            hearth.save(update_fields=["phase"])
        messages.success(request, "新值守已开灶")
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
            break

    if _wants_htmx(request):
        hearth.refresh_from_db()
        resp = render(request, "floor/_drawer.html", _drawer_context(hearth))
        resp["HX-Trigger"] = "floor-refresh"
        return resp
    return redirect(f"/?hearth={pk}")


@login_required
@require_POST
def close_run(request, pk):
    hearth = get_object_or_404(FireHearth, pk=pk)
    open_run = hearth.open_run()
    if open_run is None:
        messages.error(request, "没有进行中的值守可收灶")
    else:
        open_run.closedAt = timezone.now()
        open_run.save(update_fields=["closedAt"])
        hearth.phase = FireHearth.PHASE_COLD
        hearth.save(update_fields=["phase"])
        messages.success(request, "值守已收灶，灶台回冷灶")

    if _wants_htmx(request):
        hearth.refresh_from_db()
        resp = render(request, "floor/_drawer.html", _drawer_context(hearth))
        resp["HX-Trigger"] = "floor-refresh"
        return resp
    return redirect(f"/?hearth={pk}")


@login_required
@require_http_methods(["GET", "POST"])
def resin_lot_feed(request):
    if request.method == "POST":
        form = ResinLotForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "来脂批已登记")
            return redirect("resin_lot_feed")
    else:
        form = ResinLotForm(
            initial={
                "receivedAt": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            }
        )

    lots = ResinLot.objects.all()[:40]
    return render(request, "resin/feed.html", {"lots": lots, "form": form})
