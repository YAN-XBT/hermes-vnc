// Desktop half of hermes-vnc: a live view of the remote screen the agent is driving.
//
// The agent needs still frames — look, decide, act — so that is all the tool path gives it.
// A person watching wants the opposite, and this pane is for them: it polls the plugin's own
// backend a few times a second and paints the picture, so you can see what the agent is doing
// on the far machine while it does it.
//
// Loaded uncompiled by the Desktop app, so no JSX: the only importable specifiers are
// `@hermes/plugin-sdk`, `react` and `react/jsx-runtime`.

import { host } from '@hermes/plugin-sdk'
import * as sdk from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useState } from 'react'

const FRAME_INTERVAL_MS = 350

// The area constants come from the SDK; the literals are the fallback so a rename upstream
// degrades to a working pane instead of a plugin that refuses to load.
const AREA_PANES = sdk.PANES_AREA || 'panes'
const AREA_ROUTES = sdk.ROUTES_AREA || 'routes'
const AREA_NAV = sdk.SIDEBAR_NAV_AREA || 'sidebar.nav'
const AREA_PALETTE = sdk.PALETTE_AREA || 'palette'
const AREA_KEYBINDS = sdk.KEYBINDS_AREA || 'keybinds'

const PATH = '/vnc-screen'

let restDoor = null // ctx.rest, captured at register()

function useFrames() {
  const [state, setState] = useState({ jpeg: null, size: null, error: null })

  useEffect(() => {
    let alive = true
    let timer = null

    const tick = async () => {
      if (!restDoor) {
        if (alive) setState((s) => ({ ...s, error: 'the plugin backend is not loaded' }))
        return
      }
      try {
        const reply = await restDoor('/frame')
        if (!alive) return
        if (reply && reply.ok) {
          setState({ jpeg: reply.jpeg, size: [reply.width, reply.height], error: null })
        } else {
          setState((s) => ({ ...s, error: (reply && reply.error) || 'no frame' }))
        }
      } catch (err) {
        if (alive) setState((s) => ({ ...s, error: String((err && err.message) || err) }))
      }
      if (alive) timer = setTimeout(tick, FRAME_INTERVAL_MS)
    }

    tick()
    return () => {
      alive = false
      if (timer) clearTimeout(timer)
    }
  }, [])

  return state
}

function LiveScreen() {
  const { jpeg, size, error } = useFrames()

  return jsxs('div', {
    className: 'flex h-full w-full flex-col gap-2 p-2 text-xs',
    children: [
      jsxs('div', {
        className: 'flex shrink-0 items-center gap-2 text-(--ui-text-tertiary)',
        children: [
          jsx('span', { className: 'font-medium text-(--ui-text-secondary)', children: 'remote screen' }),
          size ? jsx('span', { children: `${size[0]}×${size[1]}` }) : null,
          error ? jsx('span', { className: 'truncate', title: error, children: error }) : null
        ]
      }),
      jpeg
        ? jsx('img', {
            alt: 'remote screen',
            src: `data:image/jpeg;base64,${jpeg}`,
            className: 'w-full rounded border border-(--ui-border)'
          })
        : jsx('div', {
            className: 'flex flex-1 items-center justify-center text-(--ui-text-tertiary)',
            children: error ? 'no picture' : 'connecting…'
          })
    ]
  })
}

export default {
  id: 'hermes-vnc',
  name: 'VNC screen',
  // Opt-in: the pane appears in Capabilities ▸ Plugins and stays off until the user asks.
  defaultEnabled: false,

  register(ctx) {
    restDoor = (path) => ctx.rest(path)

    ctx.register({
      id: 'pane',
      area: AREA_PANES,
      title: 'VNC screen',
      data: { placement: 'right', width: '460px' },
      render: () => jsx(LiveScreen, {})
    })

    ctx.register({
      id: 'page',
      area: AREA_ROUTES,
      title: 'VNC screen',
      data: { path: PATH },
      render: () => jsx(LiveScreen, {})
    })

    ctx.register({
      id: 'nav',
      area: AREA_NAV,
      title: 'VNC screen',
      data: { path: PATH, label: 'VNC screen', codicon: 'device-desktop' }
    })

    // A pane stacks as a tab with the other panes of its role, so finding it means finding
    // that tab. These two commands are the way in that does not: one opens the sidebar the
    // pane lives in, the other goes straight to the full page.
    ctx.registerMany([
      {
        id: 'reveal',
        area: AREA_PALETTE,
        data: {
          id: 'hermes-vnc.reveal',
          label: 'Show the remote screen pane',
          keywords: ['vnc', 'remote', 'screen', 'pane', 'sidebar'],
          run: () => ctx.runAction('view.toggleRightSidebar')
        }
      },
      {
        id: 'open',
        area: AREA_PALETTE,
        data: {
          id: 'hermes-vnc.open',
          label: 'Open the remote screen',
          keywords: ['vnc', 'remote', 'screen', 'page'],
          run: () => host.navigate(PATH)
        }
      },
      {
        id: 'hotkey',
        area: AREA_KEYBINDS,
        data: {
          id: 'hermes-vnc.open',
          label: 'Open the remote screen',
          category: 'VNC screen',
          defaults: ['mod+alt+v'],
          run: () => host.navigate(PATH)
        }
      }
    ])
  }
}
