"""真实 stdio 往返验证：启动日志不能污染 MCP JSON 协议。"""
import json
import os
import sqlite3
import sys

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.parametrize('mode', ['snapshot', 'live'])
def test_stdio_initialize_list_and_call(tmp_path, caplog, mode):
    account = tmp_path / 'account'
    account.mkdir()
    source = tmp_path / 'source'
    source.mkdir()
    with sqlite3.connect(account / 'contact.db') as conn:
        conn.execute('CREATE TABLE contact(username, nick_name, remark, alias)')
        conn.execute("INSERT INTO contact VALUES ('test-user','Synthetic','','test')")
    conn.close()
    config = tmp_path / 'mcp-config.json'
    values = {'mode': mode, 'source_account_dir': str(source),
              'account_dir': str(account), 'output_dir': str(tmp_path / 'out')}
    if mode == 'live':
        db_dir = source / 'db_storage' / 'contact'
        db_dir.mkdir(parents=True)
        (account / 'contact.db').replace(db_dir / 'contact.db')
        key = tmp_path / 'db-key.txt'
        key.write_text('ab' * 32)
        values.update(db_dir=str(db_dir.parent), key_file=str(key))
    config.write_text(json.dumps(values))

    async def check():
        server = StdioServerParameters(command=sys.executable,
            args=['-m', 'wechat_extract.mcp_server', '--config', str(config)],
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        with anyio.fail_after(30):
            async with stdio_client(server) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    assert len(names) == 23
                    assert {'get_chat_history', 'decode_voice', 'get_status', 'export_media',
                            'get_new_events', 'get_recent_events'} <= names
                    result = await session.call_tool('get_contacts', {'limit': 1})
                    assert not result.isError
                    rows = json.loads(result.content[0].text)
                    assert rows[0]['username'] == 'test-user'
                    status = await session.call_tool('get_status', {})
                    assert json.loads(status.content[0].text)['mode'] == mode
    anyio.run(check)
    assert 'Failed to parse JSONRPC' not in caplog.text
