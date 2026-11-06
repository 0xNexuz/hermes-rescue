/**
 * Rescue — Desktop half.
 *
 *   • status bar: a lifebuoy with a dot (green all good · amber worth a look · red needs you now)
 *   • a "Rescue" pane: guard switch, what happened today, snapshots with one-click undo
 *   • a toast (and an OS notification while you're away) when Rescue stops a risky change
 *     or a reply fails
 *
 * Data comes from this package's backend (dashboard/plugin_api.py) through ctx.rest, so the pane
 * shows exactly what /rescue shows in chat. Loaded uncompiled: no JSX, only the three imports
 * the SDK allows.
 */

import {
  Button,
  EmptyState,
  STATUSBAR_AREAS,
  ScrollArea,
  StatusDot,
  Tip,
  host,
  queryClient,
  relativeTime,
  useMutation,
  useQuery
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'hermes-rescue'
const HEALTH_KEY = ['plugin:' + ID, 'health']
const POLL_MS = 15_000

/** Lifebuoy, 24×24, stroked with currentColor so it follows every theme. */
const LIFEBUOY = [
  'M12 3a9 9 0 1 0 0 18a9 9 0 0 0 0-18Z',
  'M12 8a4 4 0 1 0 0 8a4 4 0 0 0 0-8Z',
  'M5.6 5.6l3.6 3.6M14.8 14.8l3.6 3.6M18.4 5.6l-3.6 3.6M9.2 14.8l-3.6 3.6'
]

let pluginCtx = null

function Lifebuoy({ size = 13 }) {
  return jsx('svg', {
    width: size,
    height: size,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.8,
    strokeLinecap: 'round',
    'aria-hidden': 'true',
    children: LIFEBUOY.map((d, i) => jsx('path', { d }, i))
  })
}

function useHealth() {
  return useQuery({
    queryKey: HEALTH_KEY,
    queryFn: () => pluginCtx.rest('/health'),
    refetchInterval: POLL_MS,
    retry: 1
  })
}

function refresh() {
  queryClient.invalidateQueries({ queryKey: HEALTH_KEY })
}

function clock(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function headline(data) {
  if (!data) {
    return 'Rescue'
  }
  if (!data.guard.on) {
    return `Guard is off until ${clock(data.guard.until)}`
  }
  if (data.tone === 'bad') {
    return 'Something needs you'
  }
  if (data.tone === 'warn') {
    return 'All right — worth a look'
  }
  return 'All good'
}

function summary(counts = {}) {
  const parts = [
    ['guard_block', 'stopped'],
    ['guard_ask', 'asked you first'],
    ['failed_turn', 'failed replies explained'],
    ['degraded_turn', 'answered by a backup']
  ]
    .filter(([k]) => counts[k])
    .map(([k, label]) => `${counts[k]} ${label}`)
  return parts.length ? parts.join(' · ') : 'Nothing went wrong today.'
}

// ---- status bar -------------------------------------------------------------------------------

function Chip() {
  const { data, isError } = useHealth()
  const tone = isError ? 'muted' : data ? data.tone : 'muted'
  const tip = isError ? 'Rescue: backend not reachable (is the plugin enabled for this profile?)' : `${headline(data)} · ${summary(data && data.counts)}`

  return jsx(Tip, {
    label: tip,
    children: jsxs('span', {
      className: 'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem]',
      style: { color: 'var(--ui-text-tertiary)' },
      'aria-label': tip,
      children: [jsx(Lifebuoy, {}), jsx(StatusDot, { tone })]
    })
  })
}

// ---- pane -------------------------------------------------------------------------------------

function Section({ title, children }) {
  return jsxs('section', {
    className: 'flex flex-col gap-1.5',
    children: [
      jsx('div', {
        className: 'text-[0.6875rem] font-medium uppercase tracking-wide',
        style: { color: 'var(--ui-text-quaternary)' },
        children: title
      }),
      children
    ]
  })
}

function EventRow({ event }) {
  return jsxs('div', {
    className: 'flex flex-col gap-0.5 border-b py-1.5 text-xs last:border-b-0',
    style: { borderColor: 'var(--ui-stroke-secondary)' },
    children: [
      jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx('span', { className: 'font-medium', children: event.label }),
          jsx('span', { style: { color: 'var(--ui-text-quaternary)' }, children: relativeTime(event.ts * 1000) })
        ]
      }),
      jsx('div', { style: { color: 'var(--ui-text-secondary)' }, children: event.title }),
      event.fix
        ? jsx('div', { style: { color: 'var(--ui-text-tertiary)' }, children: `Fix: ${event.fix}` })
        : null
    ]
  })
}

function SnapshotRow({ snap, onUndo, busy }) {
  return jsxs('div', {
    className: 'flex items-center justify-between gap-2 py-1 text-xs',
    children: [
      jsxs('div', {
        className: 'flex min-w-0 flex-col',
        children: [
          jsx('span', { className: 'truncate', children: `#${snap.id} ${snap.label}` }),
          jsx('span', { style: { color: 'var(--ui-text-quaternary)' }, children: relativeTime(snap.ts * 1000) })
        ]
      }),
      jsx(Button, {
        size: 'xs',
        variant: 'outline',
        disabled: busy,
        onClick: () => onUndo(snap.id),
        children: 'Undo'
      })
    ]
  })
}

function post(path, body) {
  return pluginCtx.rest(path, { method: 'POST', body })
}

function afterAction(result) {
  refresh()
  if (result && result.message) {
    host.notify({ kind: result.ok === false ? 'warning' : 'info', title: 'Rescue', message: result.message })
  }
}

function Pane() {
  const { data, isLoading, isError } = useHealth()
  const undo = useMutation({ mutationFn: id => post('/undo', { id }), onSuccess: afterAction })
  const guard = useMutation({ mutationFn: body => post('/guard', body), onSuccess: afterAction })
  const busy = undo.isPending || guard.isPending

  if (isError) {
    return jsx('div', {
      className: 'p-3',
      children: jsx(EmptyState, {
        title: 'Rescue backend not reachable',
        description: 'Enable hermes-rescue for this profile (hermes plugins enable hermes-rescue), then reopen this pane.'
      })
    })
  }

  if (isLoading || !data) {
    return jsx('div', { className: 'p-3 text-xs', style: { color: 'var(--ui-text-tertiary)' }, children: 'Loading…' })
  }

  return jsx(ScrollArea, {
    className: 'h-full',
    children: jsxs('div', {
      className: 'flex flex-col gap-4 p-3 text-sm',
      children: [
        jsxs('div', {
          className: 'flex items-center gap-2',
          children: [
            jsx(StatusDot, { tone: data.tone, className: 'size-2' }),
            jsx('span', { className: 'font-medium', children: headline(data) })
          ]
        }),
        jsx('div', { className: 'text-xs', style: { color: 'var(--ui-text-tertiary)' }, children: summary(data.counts) }),
        jsx(Section, {
          title: 'Guard',
          children: jsxs('div', {
            className: 'flex items-center justify-between gap-2 text-xs',
            children: [
              jsx('span', {
                style: { color: 'var(--ui-text-secondary)' },
                children: data.guard.on
                  ? 'On — the agent cannot delete or hand-edit Hermes’s own data.'
                  : 'Off — changes go through, with a snapshot first.'
              }),
              jsx(Button, {
                size: 'xs',
                variant: data.guard.on ? 'outline' : 'default',
                disabled: busy,
                onClick: () => guard.mutate(data.guard.on ? { minutes: 10 } : { lock: true }),
                children: data.guard.on ? 'Off for 10 min' : 'Turn back on'
              })
            ]
          })
        }),
        jsx(Section, {
          title: 'Snapshots',
          children: data.snapshots.length
            ? data.snapshots.map(s => jsx(SnapshotRow, { snap: s, busy, onUndo: id => undo.mutate(id) }, s.id))
            : jsx('div', {
                className: 'text-xs',
                style: { color: 'var(--ui-text-tertiary)' },
                children: 'None yet. One is saved before every risky change Rescue lets through.'
              })
        }),
        jsx(Section, {
          title: 'Recent',
          children: data.events.length
            ? jsx('div', { children: data.events.map(e => jsx(EventRow, { event: e }, e.id)) })
            : jsx('div', {
                className: 'text-xs',
                style: { color: 'var(--ui-text-tertiary)' },
                children: 'Nothing has gone wrong this week.'
              })
        })
      ]
    })
  })
}

// ---- live events from the agent half ----------------------------------------------------------

const ALERT_TITLES = {
  guard_block: 'Rescue stopped a risky change',
  failed_turn: 'A reply failed — Rescue found why'
}

function onRescueEvent(event) {
  refresh()
  const payload = (event && event.payload) || {}
  const title = ALERT_TITLES[payload.kind]
  if (!title) {
    return
  }
  host.notify({ kind: 'warning', title, message: payload.title || '' })
  // Only shown while you're away from Hermes; you can silence it in Capabilities → Plugins.
  pluginCtx.os.notify({ title, body: payload.title || '' })
}

export default {
  id: ID,
  name: 'Rescue',
  register(ctx) {
    pluginCtx = ctx
    ctx.registerMany([
      {
        id: 'chip',
        area: STATUSBAR_AREAS.right,
        order: 110,
        render: () => jsx(Chip, {})
      },
      {
        id: 'pane',
        area: 'panes',
        title: 'Rescue',
        data: { placement: 'right', width: '300px' },
        render: () => jsx(Pane, {})
      }
    ])
    ctx.onEvent('plugin.' + ID + '.changed', onRescueEvent)
  }
}
