import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { CheckCircle2, Clock, RefreshCw } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { formatCurrency } from '@/lib/format'
import type { RecurringMonthlyProgress } from '@/types'

interface RecurringProgressCardProps {
  progress?: RecurringMonthlyProgress
  isLoading?: boolean
  locale?: string
  mask: (val: string) => string
  variant?: 'page' | 'dashboard'
}

export function RecurringProgressCard({
  progress,
  isLoading,
  locale = 'pt-BR',
  mask,
  variant = 'page',
}: RecurringProgressCardProps) {
  const { t } = useTranslation()

  if (isLoading) {
    if (variant === 'dashboard') return null
    return (
      <div className="bg-card rounded-xl border border-border shadow-sm p-5 mb-5">
        <div className="space-y-3">
          <Skeleton className="h-4 w-32" />
          <div className="grid grid-cols-3 gap-4">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
          <Skeleton className="h-2 w-full" />
        </div>
      </div>
    )
  }

  if (!progress) return null

  const currency = progress.currency || 'USD'
  const isAllPaid = progress.count_total > 0 && progress.remaining <= 0.009
  const hasNoBills = progress.count_total === 0

  if (variant === 'dashboard') {
    // If no recurring bills this month, don't show on dashboard to keep it compact
    if (hasNoBills) return null

    return (
      <div className="bg-card rounded-xl border border-border shadow-sm mb-5">
        <div className="px-5 py-4 border-b border-border flex items-center justify-between">
          <div className="flex items-center gap-2">
            <RefreshCw size={15} className="text-muted-foreground" />
            <p className="text-sm font-semibold text-foreground">
              {t('recurring.dashboardTitle')}
            </p>
          </div>
          <Link
            to="/recurring"
            className="text-xs font-medium text-primary hover:underline"
          >
            {t('recurring.viewAll')} &rarr;
          </Link>
        </div>
        <div className="p-5">
          <div className="flex flex-wrap items-baseline justify-between gap-2 mb-2">
            <div>
              <p className="text-xl font-bold tabular-nums text-foreground">
                {isAllPaid ? (
                  <span className="text-emerald-600 flex items-center gap-1.5">
                    <CheckCircle2 size={18} />
                    {t('recurring.allBillsPaid')}
                  </span>
                ) : (
                  <span>
                    {mask(formatCurrency(progress.remaining, currency, locale))}{' '}
                    <span className="text-sm font-normal text-muted-foreground">
                      {t('recurring.remaining').toLowerCase()}
                    </span>
                  </span>
                )}
              </p>
            </div>
            <p className="text-xs text-muted-foreground tabular-nums">
              {t('recurring.paidOfTotal', {
                paid: mask(formatCurrency(progress.paid, currency, locale)),
                total: mask(formatCurrency(progress.total, currency, locale)),
              })}
              {' · '}
              {t('recurring.billsPaidCount', {
                paid: progress.count_paid,
                total: progress.count_total,
              })}
            </p>
          </div>
          <div className="w-full h-2 bg-muted/60 rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full transition-all duration-300 ${
                isAllPaid ? 'bg-emerald-500' : 'bg-primary'
              }`}
              style={{ width: `${Math.min(100, Math.max(0, progress.percentage))}%` }}
            />
          </div>
        </div>
      </div>
    )
  }

  // Variant "page" for /recurring
  return (
    <div className="bg-card rounded-xl border border-border shadow-sm p-4 sm:p-5 mb-5">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <Clock size={16} className="text-primary" />
          <h2 className="text-sm font-semibold text-foreground">
            {t('recurring.thisMonth')}
          </h2>
        </div>
        <span className="text-xs text-muted-foreground tabular-nums">
          {progress.month}
        </span>
      </div>

      {hasNoBills ? (
        <div className="py-2 text-center text-sm text-muted-foreground">
          {t('recurring.noBillsThisMonth')}
        </div>
      ) : (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-4">
            <div className="rounded-lg bg-muted/40 p-3">
              <p className="text-xs font-medium text-muted-foreground mb-1">
                {t('recurring.total')}
              </p>
              <p className="text-lg font-bold tabular-nums text-foreground">
                {mask(formatCurrency(progress.total, currency, locale))}
              </p>
              <p className="text-[11px] text-muted-foreground mt-0.5">
                {progress.count_total} {t('recurring.title').toLowerCase()}
              </p>
            </div>

            <div className="rounded-lg bg-emerald-50 dark:bg-emerald-950/20 border border-emerald-100 dark:border-emerald-900/30 p-3">
              <p className="text-xs font-medium text-emerald-800 dark:text-emerald-300 mb-1">
                {t('recurring.paid')}
              </p>
              <p className="text-lg font-bold tabular-nums text-emerald-600 dark:text-emerald-400">
                {mask(formatCurrency(progress.paid, currency, locale))}
              </p>
              <p className="text-[11px] text-emerald-700/80 dark:text-emerald-400/70 mt-0.5">
                {t('recurring.billsPaidCount', {
                  paid: progress.count_paid,
                  total: progress.count_total,
                })}
              </p>
            </div>

            <div className="rounded-lg bg-muted/40 p-3">
              <p className="text-xs font-medium text-muted-foreground mb-1">
                {t('recurring.remaining')}
              </p>
              <p className="text-lg font-bold tabular-nums text-foreground">
                {mask(formatCurrency(progress.remaining, currency, locale))}
              </p>
              <p className="text-[11px] text-muted-foreground mt-0.5">
                {progress.count_remaining} {t('recurring.remaining').toLowerCase()}
              </p>
            </div>
          </div>

          <div className="space-y-1.5">
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span className="font-medium text-foreground">
                {progress.percentage.toFixed(0)}% {t('recurring.paid').toLowerCase()}
              </span>
              <span>
                {t('recurring.paidOfTotal', {
                  paid: mask(formatCurrency(progress.paid, currency, locale)),
                  total: mask(formatCurrency(progress.total, currency, locale)),
                })}
              </span>
            </div>
            <div className="w-full h-2 bg-muted/60 rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full transition-all duration-300 ${
                  isAllPaid ? 'bg-emerald-500' : 'bg-primary'
                }`}
                style={{ width: `${Math.min(100, Math.max(0, progress.percentage))}%` }}
              />
            </div>
          </div>
        </>
      )}
    </div>
  )
}
