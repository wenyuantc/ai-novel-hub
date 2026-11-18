import { Alert } from 'antd'
import type { OperationError } from './selection'

export function OperationAlert({ error, uncertain }: { error: OperationError; uncertain?: string }) {
  return <Alert showIcon type={error.conflict ? 'warning' : 'error'} message={error.text} description={
    error.conflicts.length > 0 ? <ul className="book-search-conflicts">
      {error.conflicts.map((chapter, index) => <li key={index}>{chapter}</li>)}
    </ul> : error.unknown ? uncertain : undefined
  } />
}
