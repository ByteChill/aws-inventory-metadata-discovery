import { useStyletron } from 'baseui';
import { Button, KIND, SIZE } from 'baseui/button';
import { Checkbox } from 'baseui/checkbox';
import { ChevronDown } from 'baseui/icon';
import { Input } from 'baseui/input';
import { PLACEMENT, StatefulPopover } from 'baseui/popover';
import { useState } from 'react';

import { useI18n } from '../i18n';

export interface Option {
  id: string;
  label: string;
}

interface Props {
  placeholder: string;
  options: Option[];
  selected: string[];
  onChange: (selected: string[]) => void;
  /** what the closed control says when something is picked (default: first label +N) */
  summarize?: (selected: string[]) => string;
}

// long lists (accounts) get a search box
const SEARCH_FROM = 8;

/** A filter whose values are ticked in a list; each tick applies at once. */
export function CheckFilter({ placeholder, options, selected, onChange, summarize }: Props) {
  const [css, theme] = useStyletron();
  const labelOf = (id: string) => options.find((o) => o.id === id)?.label ?? id;
  const summary = selected.length === 0 ? null : summarize ? summarize(selected) : selected.length === 1 ? labelOf(selected[0]) : `${labelOf(selected[0])} +${selected.length - 1}`;

  return (
    <StatefulPopover
      placement={PLACEMENT.bottomLeft}
      content={() => <CheckList options={options} selected={selected} onChange={onChange} />}
      overrides={{ Body: { style: { zIndex: 10 } } }}
    >
      <button
        type="button"
        className={css({
          all: 'unset',
          boxSizing: 'border-box',
          cursor: 'pointer',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: theme.sizing.scale300,
          width: '100%',
          height: '36px',
          paddingLeft: theme.sizing.scale500,
          paddingRight: theme.sizing.scale300,
          borderRadius: theme.borders.inputBorderRadius,
          backgroundColor: theme.colors.inputFill,
          ...theme.typography.ParagraphSmall,
          color: summary ? theme.colors.contentPrimary : theme.colors.contentTertiary,
        })}
      >
        <span className={css({ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' })}>{summary ?? placeholder}</span>
        <ChevronDown size={20} color={theme.colors.contentPrimary} />
      </button>
    </StatefulPopover>
  );
}

function CheckList({ options, selected, onChange }: Omit<Props, 'placeholder'>) {
  const [css, theme] = useStyletron();
  const { t } = useI18n();
  const [query, setQuery] = useState('');
  // the popover keeps this list mounted, so ticks are tracked here as well
  const [ticked, setTicked] = useState(selected);
  const shown = options.filter((o) => o.label.toLowerCase().includes(query.trim().toLowerCase()));
  const toggle = (id: string) => {
    const next = ticked.includes(id) ? ticked.filter((x) => x !== id) : [...ticked, id];
    setTicked(next);
    onChange(next);
  };

  return (
    <div className={css({ padding: theme.sizing.scale400, minWidth: '240px', maxWidth: '360px' })}>
      {options.length > SEARCH_FROM ? (
        <div className={css({ marginBottom: theme.sizing.scale300 })}>
          <Input size={SIZE.compact} value={query} placeholder={t('Search', '搜索')} onChange={(e) => setQuery(e.currentTarget.value)} clearable autoFocus />
        </div>
      ) : null}
      <div className={css({ maxHeight: '320px', overflowY: 'auto', display: 'grid', rowGap: theme.sizing.scale200 })}>
        {shown.map((o) => (
          <Checkbox key={o.id} checked={ticked.includes(o.id)} onChange={() => toggle(o.id)}>
            <span className={css({ ...theme.typography.ParagraphSmall })}>{o.label}</span>
          </Checkbox>
        ))}
        {!shown.length ? <span className={css({ ...theme.typography.ParagraphSmall, color: theme.colors.contentTertiary })}>{t('No match', '没有匹配项')}</span> : null}
      </div>
      {ticked.length ? (
        <div className={css({ marginTop: theme.sizing.scale300, borderTop: `1px solid ${theme.colors.borderOpaque}`, paddingTop: theme.sizing.scale300 })}>
          <Button
            size={SIZE.mini}
            kind={KIND.tertiary}
            onClick={() => {
              setTicked([]);
              onChange([]);
            }}
          >
            {t('Clear', '清除')}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
