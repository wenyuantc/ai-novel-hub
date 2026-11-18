export const AUTOSAVE_DELAY = 1_500
export const AUTOSAVE_MAX_WAIT = 10_000

/** 保留首次输入的截止时间，输入法或其他操作暂停时不发请求。 */
export class ChapterAutosaveTiming {
  private timer: ReturnType<typeof setTimeout> | null = null
  private first: number | null = null
  private last = 0
  private paused = true

  constructor(private readonly run: () => void) {}

  edit() {
    this.last = Date.now()
    this.first ??= this.last
    this.schedule()
  }

  pause(paused: boolean) {
    if (this.paused === paused) return
    this.paused = paused
    this.schedule()
  }

  clear() {
    this.first = null
    this.cancelTimer()
  }

  private cancelTimer() {
    if (this.timer !== null) clearTimeout(this.timer)
    this.timer = null
  }

  private schedule() {
    this.cancelTimer()
    if (this.paused || this.first === null) return
    const due = Math.min(this.last + AUTOSAVE_DELAY, this.first + AUTOSAVE_MAX_WAIT)
    this.timer = setTimeout(() => {
      this.clear()
      this.run()
    }, Math.max(0, due - Date.now()))
  }
}
