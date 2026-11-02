import { Alert, Space, Tabs, Typography, theme } from 'antd'

export default function BackupRestoreGuide() {
  const { token } = theme.useToken()
  const command = (text: string) => <pre style={{ padding: 16, borderRadius: token.borderRadius, background: token.colorFillQuaternary, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}><code>{text}</code></pre>
  return <Space direction="vertical" size={16} style={{ width: '100%', minWidth: 0 }}>
    <Alert showIcon type="warning" message="恢复在命令行完成，会覆盖目标数据库" description="这里提供下载与操作说明。请在隔离环境先验证备份，停止应用及所有写入，明确核对目标主机、数据库和文件路径，并保留目标原数据。后台不提供整库覆盖恢复接口。" />
    <section><h2>先确认恢复资料</h2><ol>
      <li>这是整个平台数据库：包含账号、角色权限、平台配置和全部作品。个人作品 JSON 只用于单本作品导入，两者不能互换。</li>
      <li>下载目标执行中仍可用的副本，记录数据库类型、文件实际格式和 SHA-256；本地和 S3 是同次执行的副本。</li>
      <li>单独取回原部署配置，尤其原 <code>ENCRYPTION_KEY</code>。它未包含在数据库备份中，缺少它将无法解密原模型、S3、SMTP 等已存凭据。</li>
      <li>加密文件还需要生成该文件时的独立备份密码。后来更换密码不会改变旧文件；未加密文件无需这一步。</li>
      <li>核对目标数据库引擎及版本。PostgreSQL 使用配套客户端，默认部署配套 PostgreSQL 16；备份不是跨数据库迁移工具。</li>
    </ol></section>
    <section><h2>加密文件先完整解密</h2><Typography.Paragraph>按下载的实际格式选择输出扩展名。密码由 GPG 交互询问，不写入命令参数。以下示例文件名需替换成实际文件名：</Typography.Paragraph>
      {command('gpg --output backup.dump --decrypt backup.dump.gpg\n# SQLite 对应：\ngpg --output backup.sqlite3 --decrypt backup.sqlite3.gpg')}
      <Alert type="info" showIcon message="仅在命令成功退出且完整性校验通过后继续" description="密码错误或文件损坏时停止，删除不完整的解密输出并重新核实。不要将尚未校验完成的解密流直接导入数据库。明文恢复文件仅存于私有目录，恢复完成后及时清理。" />
    </section>
    <Tabs items={[
      { key: 'postgresql', label: 'PostgreSQL 恢复', children: <section><h2>PostgreSQL 自定义格式</h2><ol>
        <li>停止应用，准备已核对的空目标数据库及有恢复权限的数据库账号。备份含应用角色数据，不包含 PostgreSQL 集群账号或部署环境。</li>
        <li>检查归档目录，再导入目标数据库；密码通过客户端交互输入。命令中的占位值和文件名必须替换。</li>
      </ol>{command('pg_restore --list backup.dump\npg_restore --exit-on-error --no-owner -W \\\n  --host=目标主机 --username=恢复账号 --dbname=已核对的空目标数据库 backup.dump')}
        <Typography.Paragraph>确认命令成功，检查账号、角色、平台配置、作品和引用；为应用配置恢复目标的 DATABASE_URL 及原 ENCRYPTION_KEY，再启动应用执行迁移并检查健康状态。恢复前先对照所用应用版本与数据库迁移兼容性。</Typography.Paragraph></section> },
      { key: 'sqlite', label: 'SQLite 恢复', children: <section><h2>SQLite 完整文件</h2><ol>
        <li>停止应用，核对源文件和目标路径。为恢复创建新的目标数据库文件，避免原库的 WAL / SHM 与恢复文件混用。</li>
        <li>校验备份并恢复到新目标，命令中的绝对路径需替换。不要在应用写入时复制覆盖数据库。</li>
      </ol>{command('sqlite3 /私有目录/backup.sqlite3 "PRAGMA integrity_check;"\nsqlite3 /已核对目录/restored.sqlite3 ".restore \'/私有目录/backup.sqlite3\'"\nsqlite3 /已核对目录/restored.sqlite3 "PRAGMA integrity_check; PRAGMA foreign_key_check;"')}
        <Typography.Paragraph>确认完整性结果为 ok、外键检查无异常，验证账号、权限、配置与作品。将应用 DATABASE_URL 指向新文件，保持正确文件权限及原 ENCRYPTION_KEY，再启动应用执行迁移并核对健康状态。</Typography.Paragraph></section> },
    ]} />
    <Typography.Paragraph type="secondary">恢复后的执行历史反映数据库快照时刻。服务启动会将遗留活动执行标记为中断并核验已知副本；不会重放未知上传或邮件。恢复后应再生成一份新备份，确认后续计划和存储可用。</Typography.Paragraph>
  </Space>
}
