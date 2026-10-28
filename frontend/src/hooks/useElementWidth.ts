import { useEffect, useRef, useState } from 'react'

/** 元素当前宽度（px，ResizeObserver 实时更新；挂载前为 0）。用于按容器而非视口宽度决定布局。 */
export function useElementWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState(0)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const observer = new ResizeObserver(([entry]) => setWidth(Math.round(entry.contentRect.width)))
    observer.observe(el)
    return () => observer.disconnect()
  }, [])
  return [ref, width] as const
}
