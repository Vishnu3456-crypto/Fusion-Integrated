from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from core.api.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from modules.directory import contracts as directory
from modules.employee_leave.domain import workflow
from modules.employee_leave.models import (
    LeaveBalance,
    LeaveDecision,
    LeaveHoliday,
    LeaveLedgerEntry,
    LeavePolicy,
    LeaveRequest,
    LeaveYearEndRun,
)

P_APPLY = "employee_leave.request.create"
P_VIEW_ALL = "employee_leave.request.view_all"
P_UNIT_HEAD = "employee_leave.request.review_unit"
P_ESTABLISHMENT = "employee_leave.request.route"
P_FINAL = "employee_leave.request.sanction"
P_ADMIN = "employee_leave.configuration.manage"
P_BALANCE = "employee_leave.balance.manage"
P_SUBSTITUTE = "employee_leave.request.respond_substitute"
P_OFFLINE = "employee_leave.record.offline"


def require(actor, *permissions):
    if actor is None or not actor.has_any_permission(*permissions):
        raise PermissionDeniedError(
            "You are not authorized to perform this action.", code="permission_denied"
        )


def visible_requests(actor):
    rows = LeaveRequest.objects.select_related("policy", "parent").prefetch_related("decisions")
    if actor is None:
        return rows.none()
    if actor.has_any_permission(P_VIEW_ALL, P_UNIT_HEAD, P_ESTABLISHMENT, P_FINAL):
        if actor.has_permission(P_VIEW_ALL):
            return rows
        return rows.filter(
            Q(employee_id=actor.user_id)
            | Q(assigned_user_id=actor.user_id)
            | Q(assigned_role__in=_roles_for(actor))
        )
    return rows.filter(Q(employee_id=actor.user_id) | Q(assigned_user_id=actor.user_id))


def _roles_for(actor):
    role_map = {
        "substitute": P_SUBSTITUTE,
        "unit_head": P_UNIT_HEAD,
        "establishment": P_ESTABLISHMENT,
        "sanctioning_authority": P_FINAL,
    }
    roles = [role for role, permission in role_map.items() if actor.has_permission(permission)]
    if actor.has_permission(P_FINAL):
        roles.extend(
            role
            for role in LeavePolicy.objects.filter(requires_higher_sanction=True).values_list(
                "hierarchy__final_role", flat=True
            )
            if role
        )
    return roles


def assigned_tasks(actor):
    return (
        visible_requests(actor)
        .filter(Q(assigned_user_id=actor.user_id) | Q(assigned_role__in=_roles_for(actor)))
        .exclude(
            status__in=(
                "approved",
                "rejected",
                "withdrawn",
                "cancelled",
                "resumption_reviewed",
            )
        )
    )


def _policy(leave_type, leave_year):
    policy = LeavePolicy.objects.filter(
        leave_type=leave_type, leave_year=leave_year, enabled=True
    ).first()
    if policy is None:
        raise ConflictError(
            "No active policy is configured for this leave type and year.",
            code="leave_policy_missing",
        )
    return policy


def _require_employee(user_id, *, label):
    person = directory.get_users([user_id]).get(user_id)
    if person is None or not person.is_active or person.kind not in {"faculty", "staff"}:
        raise ConflictError(
            f"{label} must be an active Fusion employee.", code="employee_not_eligible"
        )


def _reserved_days(user_id, policy_id):
    total = LeaveRequest.objects.filter(
        employee_id=user_id,
        policy_id=policy_id,
        request_kind__in=("new", "extension"),
        status__in=(
            "substitute_pending",
            "substitute_declined",
            "unit_head_pending",
            "establishment_pending",
            "final_pending",
        ),
    ).aggregate(total=Sum("charged_days"))["total"]
    return total or Decimal("0")


def _assign(request, status, policy):
    request.status = status
    if status == "substitute_pending":
        request.assigned_user_id = request.substitute_id
        request.assigned_role = "substitute"
    elif status == "unit_head_pending":
        request.assigned_role = "unit_head"
        request.assigned_user_id = None
    elif status == "establishment_pending":
        request.assigned_role = "establishment"
        request.assigned_user_id = None
    elif status == "final_pending":
        request.assigned_role = str(policy.hierarchy.get("final_role", "sanctioning_authority"))
        request.assigned_user_id = None
    elif status == "cancellation_unit_head_pending":
        request.assigned_role = "unit_head"
        request.assigned_user_id = None
    elif status == "cancellation_establishment_pending":
        request.assigned_role = "establishment"
        request.assigned_user_id = None
    elif status == "cancellation_final_pending" or status == "resumption_pending":
        request.assigned_role = str(policy.hierarchy.get("final_role", "sanctioning_authority"))
        request.assigned_user_id = None
    else:
        request.assigned_role = ""
        request.assigned_user_id = None


@transaction.atomic
def submit(
    *,
    actor,
    leave_type,
    start_date,
    end_date,
    reason,
    leave_year=None,
    half_day=False,
    substitute_id=None,
    station_leave_details="",
):
    require(actor, P_APPLY)
    year = leave_year or start_date.year
    policy = _policy(leave_type, year)
    if policy.faculty_only and getattr(actor, "kind", "") not in ("faculty", "staff"):
        raise ConflictError(
            "This leave category is available to faculty only.", code="leave_category_ineligible"
        )
    facts = workflow.RequestFacts(
        leave_type=leave_type,
        start_date=start_date,
        end_date=end_date,
        half_day=half_day,
        requires_substitute=policy.requires_substitute,
        higher_sanction=policy.requires_higher_sanction,
        is_director="director" in {r.lower() for r in actor.roles},
    )
    days = workflow.charged_days(start_date, end_date, half_day=half_day)
    if (
        leave_type == "RH"
        and not LeaveHoliday.objects.filter(
            leave_year=year, day=start_date, restricted=True, published=True
        ).exists()
    ):
        raise ConflictError(
            "Restricted Holiday must be selected from the published calendar.",
            code="restricted_holiday_unavailable",
        )
    if policy.max_continuous_days and days > policy.max_continuous_days:
        raise ConflictError(
            "The requested period exceeds the configured continuous-leave limit.",
            code="continuous_limit_exceeded",
        )
    if not (reason or "").strip():
        raise ConflictError("A reason is required.", code="reason_required")
    if policy.requires_substitute and not substitute_id:
        raise ConflictError(
            "Nominate an eligible substitute before submitting.", code="substitute_required"
        )
    if substitute_id == actor.user_id:
        raise ConflictError(
            "You cannot nominate yourself as substitute.", code="invalid_substitute"
        )
    if substitute_id is not None:
        _require_employee(substitute_id, label="The substitute")
    if LeaveRequest.objects.filter(
        employee_id=actor.user_id,
        request_kind__in=("new", "extension", "offline"),
        status__in=(
            "substitute_pending",
            "substitute_declined",
            "unit_head_pending",
            "establishment_pending",
            "final_pending",
            "approved",
            "cancellation_pending",
            "resumption_pending",
        ),
        start_date__lte=end_date,
        end_date__gte=start_date,
    ).exists():
        raise ConflictError(
            "This leave period overlaps another active request.", code="leave_period_overlap"
        )

    balance, _ = LeaveBalance.objects.select_for_update().get_or_create(
        user_id=actor.user_id, policy=policy
    )
    available = balance.available - _reserved_days(actor.user_id, policy.pk)
    if available < days:
        raise ConflictError(
            "The available leave balance is insufficient.",
            code="insufficient_leave_balance",
            details=[{"available": str(available), "requested": str(days)}],
        )

    status = workflow.initial_state(facts)
    request = LeaveRequest.objects.create(
        employee_id=actor.user_id,
        policy=policy,
        leave_type=leave_type,
        start_date=start_date,
        end_date=end_date,
        charged_days=days,
        reason=reason.strip(),
        station_leave_details=station_leave_details.strip(),
        substitute_id=substitute_id,
        half_day=half_day,
        status=status,
    )
    _assign(request, status, policy)
    request.save(update_fields=("assigned_role", "assigned_user_id"))
    LeaveDecision.objects.create(
        request=request,
        actor_user_id=actor.user_id,
        actor_role=actor.active_role or actor.kind,
        action="submit",
        from_status="",
        to_status=status,
        comment="",
    )
    return request


@transaction.atomic
def request_extension(*, actor, request_id, start_date, end_date, reason, substitute_id=None):
    require(actor, P_APPLY)
    parent = (
        LeaveRequest.objects.select_for_update()
        .filter(
            pk=request_id,
            employee_id=actor.user_id,
            status="approved",
            request_kind__in=("new", "offline", "extension"),
        )
        .first()
    )
    if parent is None:
        raise NotFoundError("No approved leave is available for extension.")
    today = timezone.localdate()
    if not parent.start_date <= today <= parent.end_date:
        raise ConflictError(
            "An extension is available only while the approved leave is ongoing.",
            code="extension_not_ongoing",
        )
    workflow.validate_extension(leave_type=parent.leave_type, start=start_date, end=end_date)
    if start_date.toordinal() != parent.end_date.toordinal() + 1:
        raise ConflictError(
            "An extension must begin the day after the approved end date.",
            code="extension_must_be_contiguous",
        )
    if LeaveRequest.objects.filter(
        employee_id=actor.user_id,
        request_kind="extension",
        status__in=(
            "substitute_pending",
            "substitute_declined",
            "unit_head_pending",
            "establishment_pending",
            "final_pending",
            "approved",
        ),
        start_date__lte=end_date,
        end_date__gte=start_date,
    ).exists():
        raise ConflictError(
            "Another extension already covers this period.", code="extension_period_overlap"
        )
    days = workflow.charged_days(start_date, end_date)
    balance = (
        LeaveBalance.objects.select_for_update()
        .filter(user_id=actor.user_id, policy=parent.policy)
        .first()
    )
    available = (
        balance.available - _reserved_days(actor.user_id, parent.policy_id)
        if balance is not None
        else Decimal("0")
    )
    if balance is None or available < days:
        raise ConflictError(
            "The available leave balance is insufficient for this extension.",
            code="insufficient_leave_balance",
        )
    if not (reason or "").strip():
        raise ConflictError("A reason is required.", code="reason_required")
    if parent.policy.requires_substitute and not substitute_id:
        raise ConflictError(
            "Nominate an eligible substitute before submitting.", code="substitute_required"
        )
    if substitute_id is not None:
        if substitute_id == actor.user_id:
            raise ConflictError(
                "You cannot nominate yourself as substitute.", code="invalid_substitute"
            )
        _require_employee(substitute_id, label="The substitute")
    status = "substitute_pending" if parent.policy.requires_substitute else "unit_head_pending"
    extension = LeaveRequest.objects.create(
        employee_id=actor.user_id,
        policy=parent.policy,
        parent=parent,
        request_kind="extension",
        leave_type=parent.leave_type,
        start_date=start_date,
        end_date=end_date,
        charged_days=days,
        reason=reason.strip(),
        substitute_id=substitute_id,
        status=status,
    )
    _assign(extension, status, parent.policy)
    extension.save(update_fields=("assigned_role", "assigned_user_id"))
    LeaveDecision.objects.create(
        request=extension,
        actor_user_id=actor.user_id,
        actor_role=actor.active_role or actor.kind,
        action="submit_extension",
        from_status="",
        to_status=status,
    )
    return extension


@transaction.atomic
def request_cancellation(*, actor, request_id):
    parent = (
        LeaveRequest.objects.select_for_update()
        .filter(
            pk=request_id,
            employee_id=actor.user_id,
            status="approved",
            request_kind__in=("new", "offline", "extension"),
        )
        .first()
    )
    if (
        parent is None
        or parent.start_date <= timezone.localdate()
        or LeaveRequest.objects.filter(
            parent=parent,
            request_kind="cancellation",
            status__in=(
                "cancellation_unit_head_pending",
                "cancellation_establishment_pending",
                "cancellation_final_pending",
            ),
        ).exists()
    ):
        raise ConflictError(
            "Only approved leave that has not started can be cancelled.",
            code="cancellation_not_available",
        )
    cancellation = LeaveRequest.objects.create(
        employee_id=parent.employee_id,
        policy=parent.policy,
        parent=parent,
        request_kind="cancellation",
        leave_type=parent.leave_type,
        start_date=parent.start_date,
        end_date=parent.end_date,
        charged_days=parent.charged_days,
        reason="Employee requested cancellation",
        status="cancellation_unit_head_pending",
    )
    _assign(cancellation, cancellation.status, parent.policy)
    cancellation.save(update_fields=("assigned_role", "assigned_user_id"))
    LeaveDecision.objects.create(
        request=cancellation,
        actor_user_id=actor.user_id,
        actor_role=actor.active_role or actor.kind,
        action="request_cancellation",
        from_status="",
        to_status=cancellation.status,
    )
    return cancellation


@transaction.atomic
def act(
    *,
    request_id,
    actor,
    action,
    comment="",
    replacement_substitute_id=None,
    actual_resume_date=None,
    actual_resume_details="",
):
    request = visible_requests(actor).select_for_update().filter(pk=request_id).first()
    if request is None:
        raise NotFoundError("No such leave request.")
    role = actor.active_role or "employee"
    own = request.employee_id == actor.user_id
    before = request.status

    if action == "withdraw":
        if not own:
            raise PermissionDeniedError(
                "You may withdraw only your own request.", code="permission_denied"
            )
        if before not in {
            "substitute_pending",
            "substitute_declined",
            "unit_head_pending",
            "establishment_pending",
            "final_pending",
        }:
            raise ConflictError(
                "Only a pending request can be withdrawn.", code="request_not_pending"
            )
        after = "withdrawn"
    elif action == "request_cancellation":
        if not own:
            raise PermissionDeniedError(
                "You may cancel only your own leave.", code="permission_denied"
            )
        return request_cancellation(actor=actor, request_id=request_id)
    elif action == "request_resumption":
        if not own or before != "approved":
            raise ConflictError(
                "Resumption can be recorded only against your approved leave.",
                code="resumption_not_available",
            )
        if actual_resume_date is None:
            raise ConflictError(
                "Provide the actual date of resumption.", code="resumption_date_required"
            )
        if actual_resume_date > timezone.localdate():
            raise ConflictError(
                "The actual return date cannot be in the future.",
                code="invalid_resumption_date",
            )
        request.actual_resume_date = actual_resume_date
        request.actual_resume_details = actual_resume_details.strip()
        after = "resumption_pending"
    elif action == "nominate_substitute":
        if not own or before != "substitute_declined" or not replacement_substitute_id:
            raise ConflictError(
                "A replacement is available only after the substitute declines.",
                code="replacement_not_available",
            )
        if replacement_substitute_id == actor.user_id:
            raise ConflictError(
                "You cannot nominate yourself as substitute.", code="invalid_substitute"
            )
        request.substitute_id = replacement_substitute_id
        after = workflow.transition(before, action)
    else:
        permissions = {
            "accept_substitute": ("substitute",),
            "decline_substitute": ("substitute",),
            "recommend": (P_UNIT_HEAD,),
            "route_establishment": (P_UNIT_HEAD,),
            "approve": (P_UNIT_HEAD, P_FINAL),
            "reject": (P_UNIT_HEAD, P_ESTABLISHMENT, P_FINAL),
            "route_final": (P_ESTABLISHMENT,),
            "return_to_unit_head": (P_ESTABLISHMENT,),
            "review": (P_FINAL,),
            "approve_cancellation": (P_UNIT_HEAD, P_FINAL),
            "reject_cancellation": (P_UNIT_HEAD, P_ESTABLISHMENT, P_FINAL),
            "recommend_cancellation": (P_UNIT_HEAD,),
            "route_cancellation_establishment": (P_UNIT_HEAD,),
            "route_cancellation_final": (P_ESTABLISHMENT,),
            "return_cancellation_to_unit_head": (P_ESTABLISHMENT,),
        }
        allowed = permissions.get(action)
        if not allowed or not (
            request.assigned_user_id == actor.user_id or actor.has_any_permission(*allowed)
        ):
            raise PermissionDeniedError(
                "Your role cannot perform this workflow action.", code="permission_denied"
            )
        if (
            action
            in {
                "reject",
                "recommend",
                "route_establishment",
                "route_final",
                "return_to_unit_head",
                "reject_cancellation",
                "recommend_cancellation",
                "route_cancellation_establishment",
                "route_cancellation_final",
                "return_cancellation_to_unit_head",
            }
            and not (comment or "").strip()
        ):
            raise ConflictError("A decision comment is required.", code="comment_required")
        if (
            before == "unit_head_pending"
            and request.leave_type in {"CL", "RH"}
            and action in {"recommend", "route_establishment"}
        ):
            raise ConflictError(
                "Casual and Restricted Leave decisions are final at Unit Head.",
                code="unit_head_final_authority",
            )
        if (
            before == "cancellation_unit_head_pending"
            and request.leave_type in {"CL", "RH"}
            and action in {"recommend_cancellation", "route_cancellation_establishment"}
        ):
            raise ConflictError(
                "Casual and Restricted Leave cancellations are final at Unit Head.",
                code="unit_head_final_authority",
            )
        if (
            before == "cancellation_unit_head_pending"
            and request.policy.requires_higher_sanction
            and request.leave_type not in {"CL", "RH"}
        ):
            if action == "approve_cancellation":
                raise ConflictError(
                    "This cancellation requires a higher sanctioning authority.",
                    code="higher_sanction_required",
                )
            if action == "route_cancellation_establishment" and not request.policy.hierarchy.get(
                "establishment_required", False
            ):
                raise ConflictError(
                    "No Establishment verification step is configured for this policy.",
                    code="establishment_step_not_configured",
                )
        if (
            before == "cancellation_unit_head_pending"
            and not request.policy.requires_higher_sanction
            and action in {"recommend_cancellation", "route_cancellation_establishment"}
        ):
            raise ConflictError(
                "This cancellation is final at Unit Head under the current policy.",
                code="unit_head_final_authority",
            )
        if (
            before == "unit_head_pending"
            and request.policy.requires_higher_sanction
            and request.leave_type not in {"CL", "RH"}
        ):
            if action == "approve":
                raise ConflictError(
                    "This leave requires a higher sanctioning authority.",
                    code="higher_sanction_required",
                )
            if action == "route_establishment" and not request.policy.hierarchy.get(
                "establishment_required", False
            ):
                raise ConflictError(
                    "No Establishment verification step is configured for this policy.",
                    code="establishment_step_not_configured",
                )
        after = workflow.transition(before, action)

    request.status = after
    if after == "approved":
        request.final_decision = (
            "rejected"
            if request.request_kind == "cancellation" and action == "reject_cancellation"
            else "approved"
        )
        request.final_decision_at = timezone.now()
        if request.request_kind == "cancellation":
            _adjust_balance(request, -request.charged_days, actor)
        elif request.request_kind in {"new", "extension", "offline"}:
            _adjust_balance(request, request.charged_days, actor)
    elif after == "cancelled":
        request.final_decision = "cancelled"
        request.final_decision_at = timezone.now()
        parent = request.parent or request
        _adjust_balance(parent, -parent.charged_days, actor)
        if request.parent_id is not None:
            parent.status = "cancelled"
            parent.final_decision = "cancelled"
            parent.final_decision_at = timezone.now()
            parent.save(
                update_fields=("status", "final_decision", "final_decision_at", "updated_at")
            )
    elif after == "rejected":
        request.final_decision = "rejected"
        request.final_decision_at = timezone.now()
    elif after == "resumption_reviewed":
        # Normal return does not invent a generic Closed state or alter the account.
        if request.leave_type not in {"EL", "COL", "VL"}:
            raise ConflictError(
                "Resumption review applies to EL, COL and VL.", code="resumption_not_applicable"
            )
        if request.actual_resume_date < request.start_date:
            raise ConflictError(
                "Resumption cannot precede the approved leave start.",
                code="invalid_resumption_date",
            )
        if request.actual_resume_date != request.end_date:
            actual_days = Decimal((request.actual_resume_date - request.start_date).days + 1)
            difference = request.charged_days - actual_days
            if difference:
                _adjust_balance(request, -difference, actor)

    _assign(request, after, request.policy)
    request.save()
    LeaveDecision.objects.create(
        request=request,
        actor_user_id=actor.user_id,
        actor_role=role,
        action=action,
        from_status=before,
        to_status=after,
        comment=(comment or "").strip()[:1000],
    )
    return request


def _adjust_balance(request, used_delta, actor):
    balance = (
        LeaveBalance.objects.select_for_update()
        .filter(user_id=request.employee_id, policy=request.policy)
        .first()
    )
    if balance is None:
        raise ConflictError("The leave balance is not initialized.", code="leave_balance_missing")
    balance.used += used_delta
    balance.save(update_fields=("used", "updated_at"))
    LeaveLedgerEntry.objects.create(
        balance=balance,
        request=request,
        entry_type="deduction" if used_delta > 0 else "adjustment",
        days=abs(used_delta),
        description="Leave decision: " + request.status,
        actor_user_id=actor.user_id,
    )


@transaction.atomic
def maintain_balance(*, actor, user_id, policy_id, entry_type, days, description):
    require(actor, P_BALANCE)
    balance = (
        LeaveBalance.objects.select_for_update()
        .filter(user_id=user_id, policy_id=policy_id)
        .first()
    )
    if balance is None:
        balance = LeaveBalance.objects.create(user_id=user_id, policy_id=policy_id)
    value = Decimal(days)
    if entry_type == "credit":
        balance.credited += value
    elif entry_type == "opening":
        balance.opening_balance = value
    else:
        raise ConflictError("Entry type must be credit or opening.", code="invalid_entry_type")
    balance.save()
    return LeaveLedgerEntry.objects.create(
        balance=balance,
        entry_type=entry_type,
        days=value,
        description=description.strip()[:300],
        actor_user_id=actor.user_id,
    )


@transaction.atomic
def record_offline(
    *, actor, user_id, leave_type, leave_year, start_date, end_date, reason, half_day=False
):
    require(actor, P_OFFLINE)
    _require_employee(user_id, label="The leave record target")
    policy = _policy(leave_type, leave_year)
    days = workflow.charged_days(start_date, end_date, half_day=half_day)
    balance, _ = LeaveBalance.objects.select_for_update().get_or_create(
        user_id=user_id, policy=policy
    )
    if balance.available < days:
        raise ConflictError(
            "The available leave balance is insufficient.", code="insufficient_leave_balance"
        )
    if not (reason or "").strip():
        raise ConflictError("An administrative reason is required.", code="reason_required")
    request = LeaveRequest.objects.create(
        employee_id=user_id,
        policy=policy,
        request_kind="offline",
        leave_type=leave_type,
        start_date=start_date,
        end_date=end_date,
        charged_days=days,
        reason=reason.strip(),
        half_day=half_day,
        status="approved",
        final_decision="approved",
        final_decision_at=timezone.now(),
    )
    LeaveDecision.objects.create(
        request=request,
        actor_user_id=actor.user_id,
        actor_role=actor.active_role or actor.kind,
        action="record_offline",
        from_status="",
        to_status="approved",
        comment=reason.strip()[:1000],
    )
    _adjust_balance(request, days, actor)
    return request


def employee_balances(user_id):
    return LeaveBalance.objects.filter(user_id=user_id).select_related("policy")


def holidays(leave_year, restricted_only=False):
    rows = LeaveHoliday.objects.filter(leave_year=leave_year, published=True)
    if restricted_only:
        rows = rows.filter(restricted=True)
    return rows


@transaction.atomic
def process_year_end(*, actor, from_year):
    require(actor, P_ADMIN)
    to_year = from_year + 1
    if LeaveYearEndRun.objects.filter(from_year=from_year, to_year=to_year).exists():
        raise ConflictError(
            "Year-end processing has already run for this year.", code="year_end_already_processed"
        )

    policies = {
        (policy.leave_type, policy.leave_year): policy
        for policy in LeavePolicy.objects.filter(leave_year__in=(from_year, to_year))
    }
    sources = [policy for (kind, year), policy in policies.items() if year == from_year]
    targets = [policy for (kind, year), policy in policies.items() if year == to_year]
    if not sources or not targets:
        raise ConflictError(
            "Policies for both leave years must be configured first.", code="leave_policy_missing"
        )

    run = LeaveYearEndRun.objects.create(
        from_year=from_year, to_year=to_year, actor_user_id=actor.user_id
    )
    totals = {
        "balances_processed": 0,
        "annual_credits": Decimal("0"),
        "carried_forward": Decimal("0"),
        "converted_to_el": Decimal("0"),
        "lapsed": Decimal("0"),
    }

    for target in targets:
        for employee in directory.get_employees():
            if target.faculty_only and employee.kind != "faculty":
                continue
            balance, created = LeaveBalance.objects.get_or_create(
                user_id=employee.user_id,
                policy=target,
                defaults={"credited": target.annual_entitlement},
            )
            if created:
                totals["balances_processed"] += 1
            else:
                balance.credited += target.annual_entitlement
                balance.save(update_fields=("credited", "updated_at"))
            if target.annual_entitlement:
                LeaveLedgerEntry.objects.create(
                    balance=balance,
                    entry_type="annual_credit",
                    days=target.annual_entitlement,
                    description=f"Annual entitlement for {to_year}",
                    actor_user_id=actor.user_id,
                )
                totals["annual_credits"] += target.annual_entitlement

    for source in sources:
        for balance in LeaveBalance.objects.select_for_update().filter(policy=source):
            remaining = max(balance.available, Decimal("0"))
            target_type = source.leave_type
            amount = Decimal("0")
            if source.year_end_treatment == "carry_forward":
                amount = remaining
            elif source.year_end_treatment == "convert_to_el":
                target_type = "EL"
                amount = remaining * source.conversion_ratio
            amount = min(amount, source.carry_forward_limit)

            if amount:
                target = policies.get((target_type, to_year))
                if target is None:
                    raise ConflictError(
                        f"No {target_type} policy is configured for {to_year}.",
                        code="leave_policy_missing",
                    )
                target_balance, _ = LeaveBalance.objects.get_or_create(
                    user_id=balance.user_id, policy=target
                )
                target_balance.credited += amount
                target_balance.save(update_fields=("credited", "updated_at"))
                converted = target_type == "EL" and source.leave_type != "EL"
                LeaveLedgerEntry.objects.create(
                    balance=target_balance,
                    entry_type="year_end_conversion" if converted else "year_end_carry_forward",
                    days=amount,
                    description=f"Balance transfer from {source.leave_type} {from_year}",
                    actor_user_id=actor.user_id,
                )
                totals["converted_to_el" if converted else "carried_forward"] += amount

            balance.used += remaining
            balance.save(update_fields=("used", "updated_at"))
            if remaining:
                LeaveLedgerEntry.objects.create(
                    balance=balance,
                    entry_type="year_end_close",
                    days=remaining,
                    description=(
                        f"Year-end close for {from_year}; "
                        f"treatment={source.year_end_treatment}"
                    ),
                    actor_user_id=actor.user_id,
                )
            totals["balances_processed"] += 1
            totals["lapsed"] += remaining - min(amount, remaining)

    for key, value in totals.items():
        setattr(run, key, value)
    run.save(update_fields=(*totals.keys(), "updated_at"))
    return run
