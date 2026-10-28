import { useStyletron } from 'baseui';
import { Button, KIND, SIZE } from 'baseui/button';
import { ChevronLeft, ChevronRight } from 'baseui/icon';
import { Select } from 'baseui/select';
import { useState } from 'react';

import type { Page } from '../api/types';
import { useI18n } from '../i18n';
import { type AsyncState, useAsync } from './useAsync';

const PAGE_SIZES = [20, 50, 100];
const SIZE_KEY = 'inventory.pageSize';

function savedPageSize(): number {
  try {
    const saved = Number(localStorage.getItem(SIZE_KEY));
    return PAGE_SIZES.includes(saved) ? saved : 50;
  } catch {
    return 50;
  }
}

export interface KeysetPages<T> {
  page: AsyncState<Page<T>>;
  /** 0-based index of the shown page */
  index: number;
  pageSize: number;
  /** from the first page (the API counts only when no cursor is given); null if unknown */
  total: number | null;
  setPageSize: (size: number) => void;
  next: () => void;
  prev: () => void;
}

/**
 * Previous / next paging over a keyset cursor (the API has no offsets, so there is no
 * "jump to page N"). Cursors seen so far are kept, so going back costs one request.
 */
export function useKeysetPages<T>(
  queryKey: string,
  load: (cursor: string | undefined, limit: number) => Promise<Page<T>>,
): KeysetPages<T> {
  const [pageSize, setSize] = useState(savedPageSize);
  const key = `${queryKey}|${pageSize}`;
  // the cursors belong to one query: a new filter or page size starts again at page 1
  const [nav, setNav] = useState<{ key: string; cursors: Array<string | undefined> }>({ key, cursors: [undefined] });
  const cursors = nav.key === key ? nav.cursors : [undefined];
  const cursor = cursors[cursors.length - 1];
  // each response is tagged with its query, so a previous query's page (and total) is
  // never shown as this one's while the new request is in flight
  const result = useAsync(() => load(cursor, pageSize).then((data) => ({ key, cursor, data })), [key, cursor]);
  const current = result.data?.key === key && result.data.cursor === cursor ? result.data.data : null;
  const page: AsyncState<Page<T>> = { ...result, data: current };
  const [totals, setTotals] = useState<{ key: string; total: number | null }>({ key: '', total: null });
  if (current && current.total != null && totals.key !== key) setTotals({ key, total: current.total });

  return {
    page,
    index: cursors.length - 1,
    pageSize,
    total: totals.key === key ? totals.total : null,
    setPageSize: (size) => {
      try {
        localStorage.setItem(SIZE_KEY, String(size));
      } catch {
        // private mode: the size just isn't remembered
      }
      setSize(size);
    },
    next: () => {
      if (!page.data?.next_cursor) return;
      setNav({ key, cursors: [...cursors, page.data.next_cursor] });
      window.scrollTo({ top: 0 });
    },
    prev: () => {
      setNav({ key, cursors: cursors.slice(0, -1) });
      window.scrollTo({ top: 0 });
    },
  };
}

export function Pager<T>({ pages }: { pages: KeysetPages<T> }) {
  const [css, theme] = useStyletron();
  const { t } = useI18n();
  const { page, index, pageSize, total } = pages;
  const shown = page.data?.items.length ?? 0;
  const from = index * pageSize + (shown ? 1 : 0);
  const to = index * pageSize + shown;
  const pageCount = total != null ? Math.max(1, Math.ceil(total / pageSize)) : null;

  return (
    <div
      className={css({
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        flexWrap: 'wrap',
        gap: theme.sizing.scale400,
        marginTop: theme.sizing.scale600,
        ...theme.typography.ParagraphSmall,
        color: theme.colors.contentSecondary,
      })}
    >
      <span>
        {total != null
          ? t(`${from.toLocaleString()}–${to.toLocaleString()} of ${total.toLocaleString()}`, `第 ${from.toLocaleString()}–${to.toLocaleString()} 条，共 ${total.toLocaleString()} 条`)
          : t(`${from.toLocaleString()}–${to.toLocaleString()}`, `第 ${from.toLocaleString()}–${to.toLocaleString()} 条`)}
      </span>
      <div className={css({ display: 'flex', alignItems: 'center', gap: theme.sizing.scale300 })}>
        <span>{t('Per page', '每页')}</span>
        <div className={css({ width: '84px' })}>
          <Select
            size={SIZE.mini}
            clearable={false}
            searchable={false}
            options={PAGE_SIZES.map((n) => ({ id: n, label: String(n) }))}
            value={[{ id: pageSize, label: String(pageSize) }]}
            onChange={({ value }) => pages.setPageSize(Number(value[0]?.id ?? 50))}
          />
        </div>
        <Button size={SIZE.compact} kind={KIND.tertiary} disabled={index === 0 || page.loading} onClick={pages.prev} aria-label={t('Previous page', '上一页')}>
          <ChevronLeft size={20} />
        </Button>
        <span className={css({ fontVariantNumeric: 'tabular-nums', minWidth: '4.5em', textAlign: 'center' })}>
          {pageCount != null ? `${index + 1} / ${pageCount}` : t(`Page ${index + 1}`, `第 ${index + 1} 页`)}
        </span>
        <Button size={SIZE.compact} kind={KIND.tertiary} disabled={!page.data?.next_cursor || page.loading} onClick={pages.next} aria-label={t('Next page', '下一页')}>
          <ChevronRight size={20} />
        </Button>
      </div>
    </div>
  );
}
