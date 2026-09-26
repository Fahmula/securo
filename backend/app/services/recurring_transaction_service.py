import calendar
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_clock import app_today, get_workspace_timezone, today_in
from app.core.config import get_settings
from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.models.category import Category
from app.models.recurring_transaction import RecurringTransaction
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.recurring_transaction import (
    RecurringMonthlyProgress,
    RecurringTransactionCreate,
    RecurringTransactionUpdate,
)
from app.services import recurring_match_service
from app.services.credit_card_service import apply_effective_date
from app.services.fx_rate_service import get_rate, stamp_primary_amount


async def _verify_account_in_workspace(
    session: AsyncSession, workspace_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """Raise ValueError if the account isn't reachable from this workspace."""
    result = await session.execute(
        select(Account)
        .outerjoin(BankConnection)
        .where(
            Account.id == account_id,
            or_(
                Account.workspace_id == workspace_id,
                BankConnection.workspace_id == workspace_id,
            ),
        )
    )
    if result.scalar_one_or_none() is None:
        raise ValueError("Account not found")


async def get_recurring_transactions(
    session: AsyncSession, workspace_id: uuid.UUID
) -> list[RecurringTransaction]:
    result = await session.execute(
        select(RecurringTransaction)
        .where(RecurringTransaction.workspace_id == workspace_id)
        .order_by(RecurringTransaction.next_occurrence)
    )
    return list(result.scalars().all())


async def get_recurring_transaction(
    session: AsyncSession, recurring_id: uuid.UUID, workspace_id: uuid.UUID
) -> Optional[RecurringTransaction]:
    result = await session.execute(
        select(RecurringTransaction)
        .where(RecurringTransaction.id == recurring_id, RecurringTransaction.workspace_id == workspace_id)
    )
    return result.scalar_one_or_none()


async def create_recurring_transaction(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    data: RecurringTransactionCreate,
) -> RecurringTransaction:
    await _verify_account_in_workspace(session, workspace_id, data.account_id)
    next_occ = data.start_date
    if data.skip_first:
        next_occ = _advance_date(
            data.start_date, data.frequency,
            intended_day=data.day_of_month or data.start_date.day,
        )
    recurring = RecurringTransaction(
        user_id=user_id,
        workspace_id=workspace_id,
        account_id=data.account_id,
        category_id=data.category_id,
        description=data.description,
        amount=data.amount,
        currency=data.currency,
        type=data.type,
        frequency=data.frequency,
        weekend_adjustment=data.weekend_adjustment,
        day_of_month=data.day_of_month,
        start_date=data.start_date,
        end_date=data.end_date,
        auto_generate=data.auto_generate,
        next_occurrence=next_occ,
    )
    session.add(recurring)
    await session.flush()
    await stamp_primary_amount(
        session, user_id, recurring,
        date_field="start_date",
    )
    await session.commit()
    await session.refresh(recurring)
    return recurring


async def update_recurring_transaction(
    session: AsyncSession, recurring_id: uuid.UUID, workspace_id: uuid.UUID, data: RecurringTransactionUpdate
) -> Optional[RecurringTransaction]:
    recurring = await get_recurring_transaction(session, recurring_id, workspace_id)
    if not recurring:
        return None

    update_data = data.model_dump(exclude_unset=True)

    for required in ("weekend_adjustment", "start_date", "frequency"):
        if required in update_data and update_data[required] is None:
            raise ValueError(f"{required} is required")

    # A recurring transaction must always have an account — reject an explicit
    # null, and verify ownership of any new account_id.
    if "account_id" in update_data:
        new_account_id = update_data["account_id"]
        if new_account_id is None:
            raise ValueError("account_id is required")
        if new_account_id != recurring.account_id:
            await _verify_account_in_workspace(session, workspace_id, new_account_id)

    schedule_changed = any(
        key in update_data and update_data[key] != getattr(recurring, key)
        for key in _SCHEDULE_FIELDS
    )
    previous_next_occurrence = recurring.next_occurrence

    for key, value in update_data.items():
        setattr(recurring, key, value)

    if schedule_changed:
        recurring.next_occurrence = _first_occurrence_on_or_after(
            recurring.start_date,
            recurring.frequency,
            intended_day=recurring.day_of_month or recurring.start_date.day,
            floor=previous_next_occurrence,
        )

    await session.commit()
    await session.refresh(recurring)
    return recurring


async def delete_recurring_transaction(
    session: AsyncSession, recurring_id: uuid.UUID, workspace_id: uuid.UUID
) -> bool:
    recurring = await get_recurring_transaction(session, recurring_id, workspace_id)
    if not recurring:
        return False

    await session.delete(recurring)
    await session.commit()
    return True


def _advance_months(current: date, months: int, intended_day: int) -> date:
    """Advance by calendar months, clamping only in a shorter target month."""
    month_index = current.month - 1 + months
    year = current.year + month_index // 12
    month = month_index % 12 + 1
    day = min(intended_day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _advance_date(
    current: date, frequency: str, intended_day: Optional[int] = None,
) -> date:
    """Advance a date by the given frequency.

    For monthly, quarterly, semiannual, and yearly recurrences, ``intended_day`` is the day
    the user actually wants (e.g. 31). We cap it to the target month's length
    so short months clamp, but subsequent occurrences recover to the intended
    day when it exists again. Falls back to ``current.day`` when not provided.
    """
    if frequency == "weekly":
        return current + timedelta(weeks=1)
    if frequency == "biweekly":
        return current + timedelta(weeks=2)

    target_day = intended_day if intended_day else current.day
    if frequency == "monthly":
        return _advance_months(current, 1, target_day)
    if frequency == "quarterly":
        return _advance_months(current, 3, target_day)
    if frequency == "semiannual":
        return _advance_months(current, 6, target_day)
    if frequency == "yearly":
        year = current.year + 1
        day = min(target_day, calendar.monthrange(year, current.month)[1])
        return date(year, current.month, day)

    # Preserve the existing monthly fallback for unknown legacy values.
    return _advance_months(current, 1, target_day)


# Fields the occurrence schedule is derived from; changing any of them moves
# the next_occurrence pointer.
_SCHEDULE_FIELDS = ("start_date", "day_of_month", "frequency")


def _first_occurrence_on_or_after(
    start: date, frequency: str, intended_day: Optional[int], floor: date,
) -> date:
    """Return the first occurrence of the schedule on or after ``floor``.

    The schedule starts at ``start``, as on creation. ``floor`` is the pointer
    before the edit: every occurrence before it was already generated or
    matched, so the new pointer never moves behind it. That keeps an edit from
    backfilling past periods or repeating one that was already charged, while a
    ``start`` later than ``floor`` still defers the rule to ``start``.
    """
    current = start
    while current < floor:
        current = _advance_date(current, frequency, intended_day=intended_day)
    return current


def adjust_weekend_date(
    nominal_date: date, weekend_adjustment: str = "none"
) -> date:
    """Return the effective date without changing the nominal schedule date."""
    if weekend_adjustment not in ("none", "previous_friday", "next_monday"):
        raise ValueError(f"Unsupported weekend adjustment: {weekend_adjustment}")

    weekday = nominal_date.weekday()
    if weekend_adjustment == "none" or weekday < calendar.SATURDAY:
        return nominal_date
    if weekend_adjustment == "previous_friday":
        return nominal_date - timedelta(days=weekday - calendar.FRIDAY)
    return nominal_date + timedelta(days=7 - weekday)


def get_occurrences_in_range(
    start: date, frequency: str, end_date: Optional[date],
    range_start: date, range_end: date,
    intended_day: Optional[int] = None,
    weekend_adjustment: str = "none",
) -> list[date]:
    """Compute effective occurrence dates within ``[range_start, range_end)``.

    Schedule advancement and end-date checks use nominal dates. The two-day
    scan margin captures nominal weekend occurrences that move across a range
    boundary; filtering happens only after calculating each effective date.
    """
    day = intended_day if intended_day else start.day
    nominal_range_start = range_start - timedelta(days=2)
    nominal_range_end = range_end + timedelta(days=2)
    occurrences: list[date] = []
    current = start
    while current < nominal_range_start:
        if end_date and current > end_date:
            return occurrences
        current = _advance_date(current, frequency, intended_day=day)
    while current < nominal_range_end:
        if end_date and current > end_date:
            break
        effective_date = adjust_weekend_date(current, weekend_adjustment)
        if range_start <= effective_date < range_end:
            occurrences.append(effective_date)
        current = _advance_date(current, frequency, intended_day=day)
        if len(occurrences) > 200:
            break
    return occurrences


async def generate_pending(
    session: AsyncSession, user_id: uuid.UUID, up_to: Optional[date] = None
) -> int:
    """Generate transactions for all pending recurring transactions up to a given date.
    If up_to is None, defaults to today. This allows the dashboard to pre-generate
    transactions for future months when the user navigates ahead.
    Returns the count of transactions generated."""
    # A person's recurring rows may live in several workspaces, and each
    # workspace keeps its own calendar, so "today" is resolved per workspace.
    # The query is bounded by the latest of those days and the loop below
    # applies each row's own cutoff.
    cutoffs: dict[uuid.UUID, date] = {}
    if up_to is None:
        workspace_ids = (
            await session.execute(
                select(RecurringTransaction.workspace_id)
                .where(
                    RecurringTransaction.user_id == user_id,
                    RecurringTransaction.is_active == True,
                    RecurringTransaction.auto_generate == True,
                )
                .distinct()
            )
        ).scalars().all()
        for ws_id in workspace_ids:
            cutoffs[ws_id] = today_in(await get_workspace_timezone(session, ws_id))
        if not cutoffs:
            return 0
        latest_cutoff = max(cutoffs.values())
    else:
        latest_cutoff = up_to

    result = await session.execute(
        select(RecurringTransaction)
        .where(
            RecurringTransaction.user_id == user_id,
            RecurringTransaction.is_active == True,
            RecurringTransaction.auto_generate == True,
            or_(
                and_(
                    RecurringTransaction.weekend_adjustment == "previous_friday",
                    RecurringTransaction.next_occurrence <= latest_cutoff + timedelta(days=2),
                ),
                and_(
                    RecurringTransaction.weekend_adjustment != "previous_friday",
                    RecurringTransaction.next_occurrence <= latest_cutoff,
                ),
            ),
        )
    )
    recurring_list = list(result.scalars().all())

    count = 0
    for recurring in recurring_list:
        # Legacy rows may exist with a null account_id from before account_id
        # was required. Skip them rather than crashing on Transaction's NOT NULL
        # constraint — the user should edit the recurring to fix it.
        if recurring.account_id is None:
            continue
        cutoff = up_to or cutoffs.get(recurring.workspace_id) or app_today()
        # Generate while the effective date is due. The nominal pointer remains
        # authoritative and is the only date used for schedule advancement and
        # end-date evaluation.
        while True:
            effective_occurrence = adjust_weekend_date(
                recurring.next_occurrence, recurring.weekend_adjustment
            )
            if effective_occurrence > cutoff:
                break
            if recurring.end_date and recurring.next_occurrence > recurring.end_date:
                recurring.is_active = False
                break

            # If a real transaction (synced/imported/manual) already covers this
            # occurrence, link it to the bill instead of writing a duplicate
            # placeholder (issue #116). Otherwise materialize the placeholder,
            # stamped with the recurring link so a later synced charge merges
            # into it rather than duplicating.
            existing_real = await recurring_match_service.find_real_tx_for_occurrence(
                session, recurring, effective_occurrence
            )
            if existing_real is not None:
                existing_real.recurring_transaction_id = recurring.id
            else:
                account = await session.get(Account, recurring.account_id)
                # Only occurrences that already came due reach this point, so
                # the row is a charge that happened rather than a forecast.
                # Whether to trust that depends on who else writes to the
                # account:
                #
                # Bank-synced: the incoming charge often fails to match this
                # placeholder, and two posted rows for one charge inflate the
                # balance silently. Holding the placeholder as pending keeps
                # it out of the actuals until something confirms it, so a
                # missed match costs a visible stale row instead of a wrong
                # number. Improving the match itself is the real fix (#588);
                # this is the guard until then.
                #
                # Manual: nothing else ever writes to the account, so there is
                # no charge to duplicate against and nothing that would ever
                # post the row. Pending there buys no safety and stops the
                # balance from moving.
                is_synced_account = account is not None and account.connection_id is not None
                transaction = Transaction(
                    user_id=user_id,
                    account_id=recurring.account_id,
                    category_id=recurring.category_id,
                    description=recurring.description,
                    amount=recurring.amount,
                    currency=recurring.currency,
                    date=effective_occurrence,
                    type=recurring.type,
                    source="recurring",
                    status="pending" if is_synced_account else "posted",
                    recurring_transaction_id=recurring.id,
                )
                apply_effective_date(transaction, account)
                session.add(transaction)
                await session.flush()
                await stamp_primary_amount(session, user_id, transaction)
                count += 1

            # Advance to next occurrence
            recurring.next_occurrence = _advance_date(
                recurring.next_occurrence, recurring.frequency,
                intended_day=recurring.day_of_month or recurring.start_date.day,
            )

            # Check again if past end_date after advancing
            if recurring.end_date and recurring.next_occurrence > recurring.end_date:
                recurring.is_active = False

    await session.commit()
    return count


async def get_recurring_monthly_progress(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    month: Optional[date] = None,
    transaction_type: str = "debit",
) -> RecurringMonthlyProgress:
    """Calculate the monthly progress of recurring obligations (total expected,
    amount already posted, and amount remaining)."""
    user = await session.get(User, user_id)
    primary_currency = user.primary_currency if user else get_settings().default_currency

    if month is None:
        month = app_today().replace(day=1)
    else:
        month = month.replace(day=1)

    year = month.year
    m = month.month
    range_start = date(year, m, 1)
    range_end = date(year + 1, 1, 1) if m == 12 else date(year, m + 1, 1)
    month_str = f"{year:04d}-{m:02d}"

    # 1. Fetch active recurring rules of the requested direction in the workspace
    stmt = (
        select(RecurringTransaction)
        .outerjoin(Category, RecurringTransaction.category_id == Category.id)
        .where(
            RecurringTransaction.workspace_id == workspace_id,
            RecurringTransaction.is_active == True,
            RecurringTransaction.type == transaction_type,
            RecurringTransaction.start_date < range_end + timedelta(days=2),
            or_(
                RecurringTransaction.end_date.is_(None),
                RecurringTransaction.end_date >= range_start - timedelta(days=2),
            ),
            or_(
                RecurringTransaction.category_id.is_(None),
                Category.is_ignored.is_not(True),
            ),
        )
    )
    result = await session.execute(stmt)
    recurring_list = list(result.scalars().all())

    # 2. Fetch all real transactions in this month linked to recurring rules
    tx_stmt = select(Transaction).where(
        Transaction.workspace_id == workspace_id,
        Transaction.recurring_transaction_id.is_not(None),
        Transaction.type == transaction_type,
        Transaction.date >= range_start,
        Transaction.date < range_end,
        Transaction.is_ignored.is_not(True),
    )
    tx_result = await session.execute(tx_stmt)
    linked_txs = list(tx_result.scalars().all())

    # Group linked transactions by recurring_transaction_id
    txs_by_recurring: dict[uuid.UUID, list[Transaction]] = {}
    for tx in linked_txs:
        if tx.recurring_transaction_id:
            txs_by_recurring.setdefault(tx.recurring_transaction_id, []).append(tx)

    total_paid = Decimal("0.00")
    total_remaining = Decimal("0.00")
    count_paid = 0
    count_remaining = 0

    processed_recurring_ids: set[uuid.UUID] = set()

    for rec in recurring_list:
        processed_recurring_ids.add(rec.id)
        occurrences = get_occurrences_in_range(
            start=rec.start_date,
            frequency=rec.frequency,
            end_date=rec.end_date,
            range_start=range_start,
            range_end=range_end,
            intended_day=rec.day_of_month or rec.start_date.day,
            weekend_adjustment=rec.weekend_adjustment,
        )
        expected_count = len(occurrences)

        # Convert recurring nominal amount to primary currency
        if rec.currency == primary_currency:
            rec_rate = Decimal("1.0")
        else:
            resolved_rate = await get_rate(session, rec.currency, primary_currency, range_start)
            rec_rate = resolved_rate if resolved_rate is not None else Decimal("1.0")
        rec_nominal_primary = (rec.amount * rec_rate).quantize(Decimal("0.01"))

        rec_txs = txs_by_recurring.get(rec.id, [])
        posted_txs = [tx for tx in rec_txs if tx.status == "posted"]
        pending_txs = [tx for tx in rec_txs if tx.status == "pending"]

        # Paid from posted transactions
        for tx in posted_txs:
            if tx.amount_primary is not None:
                amt = tx.amount_primary
            elif tx.currency == primary_currency:
                amt = tx.amount
            else:
                r = tx.fx_rate_used or (await get_rate(session, tx.currency, primary_currency, tx.date)) or Decimal("1.0")
                amt = (tx.amount * Decimal(str(r))).quantize(Decimal("0.01"))
            total_paid += amt
            count_paid += 1

        # Remaining from pending transactions
        for tx in pending_txs:
            if tx.amount_primary is not None:
                amt = tx.amount_primary
            elif tx.currency == primary_currency:
                amt = tx.amount
            else:
                r = tx.fx_rate_used or (await get_rate(session, tx.currency, primary_currency, tx.date)) or Decimal("1.0")
                amt = (tx.amount * Decimal(str(r))).quantize(Decimal("0.01"))
            total_remaining += amt
            count_remaining += 1

        # Remaining unmaterialized occurrences
        unmaterialized = max(0, expected_count - len(posted_txs) - len(pending_txs))
        if unmaterialized > 0:
            total_remaining += unmaterialized * rec_nominal_primary
            count_remaining += unmaterialized

    # Also account for any linked transactions whose recurring definition is inactive/deleted
    for rec_id, tx_list in txs_by_recurring.items():
        if rec_id not in processed_recurring_ids:
            for tx in tx_list:
                if tx.amount_primary is not None:
                    amt = tx.amount_primary
                elif tx.currency == primary_currency:
                    amt = tx.amount
                else:
                    r = tx.fx_rate_used or (await get_rate(session, tx.currency, primary_currency, tx.date)) or Decimal("1.0")
                    amt = (tx.amount * Decimal(str(r))).quantize(Decimal("0.01"))

                if tx.status == "posted":
                    total_paid += amt
                    count_paid += 1
                else:
                    total_remaining += amt
                    count_remaining += 1

    total_amount = total_paid + total_remaining
    count_total = count_paid + count_remaining
    pct = (
        float((total_paid / total_amount * 100).quantize(Decimal("0.1")))
        if total_amount > Decimal("0.00")
        else 0.0
    )

    return RecurringMonthlyProgress(
        month=month_str,
        total=float(total_amount),
        paid=float(total_paid),
        remaining=float(total_remaining),
        percentage=pct,
        currency=primary_currency,
        count_total=count_total,
        count_paid=count_paid,
        count_remaining=count_remaining,
    )
