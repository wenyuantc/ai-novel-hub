"""拆书导入：上传解析与切章（splitter）、解析记录的校正与开始导入（service）、book.import 反推任务（reverse / merge）。

本包的 __init__ 保持为空：自定义正则在 spawn 子进程里执行（regex_worker），子进程只导入这个包与 regex_worker。
"""
