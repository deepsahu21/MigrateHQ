import { Layer } from '../types'

const CONFIG: Record<Layer, { label: string; cls: string }> = {
  'L1':         { label: 'L1',       cls: 'badge-l1'       },
  'L2':         { label: 'L2',       cls: 'badge-l2'       },
  'L1-fallback':{ label: 'Fallback', cls: 'badge-fallback'  },
  'none':       { label: 'None',     cls: 'badge-none'      },
}

export default function LayerBadge({ layer }: { layer: Layer }) {
  const { label, cls } = CONFIG[layer]
  return <span className={`badge ${cls}`}>{label}</span>
}
