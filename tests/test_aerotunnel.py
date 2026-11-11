import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import aerotunnel


class TestConfigParsingAndValidation(unittest.TestCase):
    def test_boilerplate_schema_and_key_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            aerotunnel.create_boilerplate_config(cfg_path)
            with open(cfg_path, "r") as f:
                raw = f.read()

            self.assertNotIn("remote_ip", raw)
            positions = [
                raw.find('"remote_host"'),
                raw.find('"remote_port"'),
                raw.find('"remote_user"'),
                raw.find('"services"'),
            ]
            self.assertTrue(all(p != -1 for p in positions))
            self.assertEqual(positions, sorted(positions))

            parsed = aerotunnel.load_json_with_comments(cfg_path)
            aerotunnel.validate_config(parsed, logger_cb=lambda _: None)
            self.assertEqual(
                list(parsed["services"].keys()),
                ["ollama", "reverse_test", "max_example"],
            )

    def test_remote_port_defaults_to_22_when_omitted(self):
        data = {
            "remote_host": "ssh.someserver.com",
            "remote_user": "username",
            "services": {
                "some_webserver": {
                    "local_port": 8080,
                    "service_host": "web.internal.someserver.com",
                    "service_port": 80,
                }
            },
        }
        aerotunnel.validate_config(data, logger_cb=lambda _: None)
        resolved = aerotunnel.resolve_ssh_target(
            "ssh.someserver.com",
            user=data.get("remote_user"),
            port=data.get("remote_port"),
            ssh_config_path="/dev/null",
        )
        self.assertEqual(resolved["port"], 22)

    def test_rejects_boolean_and_invalid_ports(self):
        for bad_port in (True, False, 0, -1, 70000):
            data = {
                "remote_host": "ssh.someserver.com",
                "remote_port": bad_port,
                "remote_user": "user",
                "services": {"s": {"local_port": 8080, "service_port": 80}},
            }
            with self.assertRaises(aerotunnel.ConfigValidationError):
                aerotunnel.validate_config(data, logger_cb=lambda _: None)

            data2 = {
                "remote_host": "ssh.someserver.com",
                "remote_port": 22,
                "remote_user": "user",
                "services": {"s": {"local_port": bad_port, "service_port": 80}},
            }
            with self.assertRaises(aerotunnel.ConfigValidationError):
                aerotunnel.validate_config(data2, logger_cb=lambda _: None)

    def test_comment_stripping_preserves_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as f:
                f.write(
                    '{\n'
                    '  // leading comment\n'
                    '  "remote_host": "host // keep # keep",\n'
                    '  "remote_user": "u", # inline hash\n'
                    '  "services": {"s": {"local_port": 8080, "service_port": 80}}\n'
                    '}\n'
                )
            parsed = aerotunnel.load_json_with_comments(cfg_path)
            self.assertEqual(parsed["remote_host"], "host // keep # keep")

    def test_format_ssh_host_ipv6(self):
        self.assertEqual(aerotunnel.format_ssh_host("::1"), "[::1]")
        self.assertEqual(aerotunnel.format_ssh_host("[::1]"), "[::1]")
        self.assertEqual(aerotunnel.format_ssh_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(
            aerotunnel.format_ssh_host("web.internal.someserver.com"),
            "web.internal.someserver.com",
        )


class TestPasswordDetectionAndDiagnostics(unittest.TestCase):
    def test_password_and_passphrase_prompt_detection(self):
        prompts = [
            "Enter passphrase for key '/home/user/.ssh/id_rsa':",
            "Enter passphrase for key '/home/user/.ssh/id_ed25519': ",
            "password: ",
            "Enter password for user@host:",
            "user@example.com's password: ",
            "(user@host) Password:",
            "Verification code: ",
        ]
        for line in prompts:
            with self.subTest(prompt=line):
                self.assertTrue(aerotunnel.is_ssh_password_prompt(line))

        non_prompts = [
            "Some normal SSH output",
            "Permission denied (publickey)",
            "Permission denied (publickey,password).",
            "Permission denied, please try again.",
            "Connection closed by 192.168.1.100 port 22",
            "bind [127.0.0.1]:8080: Address already in use",
        ]
        for line in non_prompts:
            with self.subTest(non_prompt=line):
                self.assertFalse(aerotunnel.is_ssh_password_prompt(line))

    def test_parse_port_forward_failures_distinguishes_local_remote_and_ip_octets(self):
        services = {
            "local_same": {"type": "local", "local_port": 9000, "service_port": 80},
            "remote_same": {"type": "remote", "remote_port": 9000, "service_port": 3000},
            "ip_trap": {"type": "local", "local_port": 127, "service_port": 80},
        }
        # 1. Local bind fails on 9000 while 127.0.0.1 is in line -> should flag ONLY local_same
        stderr_local = [
            "bind [127.0.0.1]:9000: Address already in use",
            "channel_setup_fwd_listener_tcpip: cannot listen to port: 9000",
            "Could not request local forwarding.",
        ]
        failed_local = aerotunnel.detect_failed_forward_services(
            stderr_local, ["local_same", "remote_same", "ip_trap"], services
        )
        self.assertEqual(failed_local, {"local_same"})

        # 2. Remote bind fails on 9000 -> should flag ONLY remote_same
        stderr_remote = [
            "Warning: remote port forwarding failed for listen port 9000",
        ]
        failed_remote = aerotunnel.detect_failed_forward_services(
            stderr_remote, ["local_same", "remote_same", "ip_trap"], services
        )
        self.assertEqual(failed_remote, {"remote_same"})


class TestTuiLayoutsAndModals(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self.tmp.name, "config.json")
        with open(self.cfg_path, "w") as f:
            json.dump(
                {
                    "remote_host": "ssh.someserver.com",
                    "remote_port": 22,
                    "remote_user": "alice",
                    "services": {
                        "ollama": {"local_port": 11434, "service_port": 11434},
                        "reverse_svc": {
                            "type": "remote",
                            "remote_port": 9100,
                            "bind_address": "0.0.0.0",
                            "service_host": "internal.local",
                            "service_port": 3000,
                        },
                        "max_example": {
                            "type": "local",
                            "local_port": 8080,
                            "bind_address": "0.0.0.0",
                            "service_host": "web.internal.someserver.com",
                            "service_port": 80,
                        },
                    },
                },
                f,
            )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_mobile_remote_displays_service_port_and_wide_does_not_clip_status(self):
        @aerotunnel.work
        async def noop_loop(self):
            return

        with patch.object(aerotunnel, "CONFIG_PATH", self.cfg_path), \
             patch.object(aerotunnel.TunnelApp, "run_ssh_loop", noop_loop):
            app = aerotunnel.TunnelApp()
            # Wide desktop (120x35): Status column must stay visible without horizontal clip
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.pause(0.1)
                table = app.query_one("#services_table", aerotunnel.DataTable)
                self.assertEqual(table.max_scroll_x, 0, "Table overflows horizontally at 120 cols!")

                # Narrow mobile (65x30): reverse_svc must show 9100 <- ...:3000, NOT 9100 <- 9100
                await pilot.resize_terminal(65, 30)
                await pilot.pause(0.1)
                self.assertEqual(table.max_scroll_x, 0, "Collapsed mobile table overflows at 65 cols!")
                rev_row = [str(v) for v in table.get_row("reverse_svc")]
                self.assertIn(":9100", rev_row[1])
                self.assertIn("3000", rev_row[1])
                self.assertNotIn("↢[/] 9100", rev_row[1])
                self.assertNotIn("↢ 9100", rev_row[1])

                # Large wide screen (170x40): shows 5 wide columns and all fit cleanly
                await pilot.resize_terminal(170, 40)
                await pilot.pause(0.1)
                self.assertEqual(len(table.columns), 5)
                self.assertEqual(table.max_scroll_x, 0)

    async def test_row_expansion_cursor_restores_to_parent_and_listen_shows_red_warning(self):
        @aerotunnel.work
        async def noop_loop(self):
            return

        with patch.object(aerotunnel, "CONFIG_PATH", self.cfg_path), \
             patch.object(aerotunnel.TunnelApp, "run_ssh_loop", noop_loop):
            app = aerotunnel.TunnelApp()
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.pause(0.1)
                table = app.query_one("#services_table", aerotunnel.DataTable)
                table.focus()
                await pilot.pause(0.05)

                # Expand row 2 ('max_example', bound to 0.0.0.0)
                await pilot.press("down", "down", "enter")
                await pilot.pause(0.05)
                self.assertIn("max_example", app.expanded_services)

                listen_row = [str(v) for v in table.get_row("max_example_sub_listen")]
                self.assertIn("red", listen_row[1])

                # Move onto sub-row and collapse with space -> cursor must stay on 'max_example' (row 2)
                await pilot.press("down")
                await pilot.pause(0.05)
                await pilot.press("space")
                await pilot.pause(0.05)
                self.assertNotIn("max_example", app.expanded_services)
                cur_rk = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
                self.assertEqual(cur_rk, "max_example")

    async def test_binding_modal_space_toggles_without_crash_or_affecting_main_table(self):
        @aerotunnel.work
        async def noop_loop(self):
            return

        with patch.object(aerotunnel, "CONFIG_PATH", self.cfg_path), \
             patch.object(aerotunnel.TunnelApp, "run_ssh_loop", noop_loop):
            app = aerotunnel.TunnelApp()
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.pause(0.1)
                await pilot.press("b")
                await pilot.pause(0.1)
                self.assertIsInstance(app.screen, aerotunnel.BindingModal)

                await pilot.press("space")
                await pilot.pause(0.1)
                # Must not have expanded background table
                self.assertEqual(app.expanded_services, set())
                self.assertEqual(app.screen.working_bindings["ollama"], "0.0.0.0")

                await pilot.press("enter")
                await pilot.pause(0.1)
                self.assertEqual(app.service_bindings["ollama"], "0.0.0.0")

    async def test_edit_config_reloads_modified_bind_address(self):
        @aerotunnel.work
        async def noop_loop(self):
            return

        with patch.object(aerotunnel, "CONFIG_PATH", self.cfg_path), \
             patch.object(aerotunnel.TunnelApp, "run_ssh_loop", noop_loop), \
             patch.object(aerotunnel, "open_editor"):
            app = aerotunnel.TunnelApp()
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.pause(0.1)
                self.assertEqual(app.service_bindings["ollama"], "127.0.0.1")
                # User toggled reverse_svc in-session, didn't touch its config
                app.service_bindings["reverse_svc"] = "127.0.0.1"

                # Simulate config edit changing ollama bind_address to 0.0.0.0
                with open(self.cfg_path, "r") as f:
                    cfg = json.load(f)
                cfg["services"]["ollama"]["bind_address"] = "0.0.0.0"
                with open(self.cfg_path, "w") as f:
                    json.dump(cfg, f)

                with patch.object(app, "suspend"), patch("aerotunnel.time.sleep"):
                    app.action_edit_config()
                await pilot.pause(0.05)

                self.assertEqual(app.service_bindings["ollama"], "0.0.0.0")
                # Untouched service preserves in-session modal override
                self.assertEqual(app.service_bindings["reverse_svc"], "127.0.0.1")

    async def test_live_log_msg_writes_to_open_log_screen(self):
        @aerotunnel.work
        async def noop_loop(self):
            return

        with patch.object(aerotunnel, "CONFIG_PATH", self.cfg_path), \
             patch.object(aerotunnel.TunnelApp, "run_ssh_loop", noop_loop):
            app = aerotunnel.TunnelApp()
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.pause(0.1)
                await pilot.press("l")
                await pilot.pause(0.05)
                rich_log = app.get_screen("log_screen").query_one(aerotunnel.RichLog)
                before = len(rich_log.lines)
                app.log_msg("LIVE_STREAM_LINE")
                await pilot.pause(0.05)
                self.assertGreater(len(rich_log.lines), before)


class TestSshLoopAndAskpass(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self.tmp.name, "config.json")
        with open(self.cfg_path, "w") as f:
            json.dump(
                {
                    "remote_host": "example.com",
                    "remote_user": "alice",
                    "services": {
                        "local_svc": {"type": "local", "local_port": 11434, "service_port": 11434},
                        "remote_svc": {"type": "remote", "remote_port": 9000, "service_port": 9000},
                    },
                },
                f,
            )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_newline_less_password_prompt_not_marked_connected(self):
        class ChunkStream:
            def __init__(self, chunks):
                self.chunks = list(chunks)
            async def read(self, n=-1):
                if self.chunks:
                    await asyncio.sleep(0.01)
                    return self.chunks.pop(0)
                await asyncio.sleep(10)
                return b""
            async def readline(self):
                return await self.read(-1)

        class MockProc:
            def __init__(self):
                self.stdout = ChunkStream([])
                self.stderr = ChunkStream([b"alice@example.com's password: "])
                self.returncode = None
                self._done = asyncio.Event()
            async def wait(self):
                await self._done.wait()
                return self.returncode
            def terminate(self):
                self.returncode = -15
                self._done.set()

        spawn_calls = []
        async def fake_exec(*cmd, **kwargs):
            spawn_calls.append((cmd, kwargs))
            return MockProc()

        with patch.object(aerotunnel, "CONFIG_PATH", self.cfg_path), \
             patch.object(aerotunnel, "STABILITY_WAIT_SEC", 0.2), \
             patch.object(aerotunnel, "RECONNECT_DELAY_SEC", 0.1), \
             patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            app = aerotunnel.TunnelApp()
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.pause(0.4)
                self.assertTrue(spawn_calls)
                cmd, kwargs = spawn_calls[0]
                # Omitted remote_port does not force -p 22 so ~/.ssh/config Port can apply
                self.assertNotIn("-p", cmd)
                # detached session + SSH_ASKPASS configured
                self.assertTrue(kwargs.get("start_new_session"))
                env = kwargs.get("env", {})
                self.assertEqual(env.get("SSH_ASKPASS_REQUIRE"), "force")
                self.assertTrue(os.path.isfile(env.get("SSH_ASKPASS", "")))
                # Must NOT lie that tunnel is connected
                self.assertNotEqual(app.stats.status, "CONNECTED")
                self.assertNotEqual(app.service_statuses["local_svc"], "active")
                self.assertTrue(
                    any("asking for a password" in l.lower() for l in app.log_history)
                )

    async def test_remote_forward_failure_enters_degraded_mode(self):
        class ChunkStream:
            def __init__(self, chunks):
                self.chunks = list(chunks)
            async def read(self, n=-1):
                if self.chunks:
                    await asyncio.sleep(0.01)
                    return self.chunks.pop(0)
                await asyncio.sleep(10)
                return b""
            async def readline(self):
                return await self.read(-1)

        class MockProc:
            def __init__(self, chunks):
                self.stdout = ChunkStream([])
                self.stderr = ChunkStream(chunks)
                self.returncode = None
                self._done = asyncio.Event()
            async def wait(self):
                await self._done.wait()
                return self.returncode
            def terminate(self):
                self.returncode = -15
                self._done.set()

        spawned_cmds = []
        async def fake_exec(*cmd, **kwargs):
            spawned_cmds.append(cmd)
            if len(spawned_cmds) == 1:
                return MockProc([b"Warning: remote port forwarding failed for listen port 9000\n"])
            return MockProc([])

        with patch.object(aerotunnel, "CONFIG_PATH", self.cfg_path), \
             patch.object(aerotunnel, "STABILITY_WAIT_SEC", 0.15), \
             patch.object(aerotunnel, "RECONNECT_DELAY_SEC", 0.05), \
             patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            app = aerotunnel.TunnelApp()
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.pause(0.5)
                self.assertGreaterEqual(len(spawned_cmds), 2)
                self.assertEqual(app.service_statuses["remote_svc"], "failed_port_in_use")
                self.assertEqual(app.service_statuses["local_svc"], "active")
                self.assertEqual(app.stats.status, "DEGRADED")
                self.assertNotIn("-R", spawned_cmds[1])

    async def test_askpass_helper_opens_modal_and_returns_password_and_cancel(self):
        class WaitProc:
            stdout = None
            stderr = None
            returncode = None
            def __init__(self):
                self.stdout = self._Empty()
                self.stderr = self._Empty()
                self._done = asyncio.Event()
            class _Empty:
                async def read(self, n=-1):
                    await asyncio.sleep(10)
                    return b""
            async def wait(self):
                await self._done.wait()
                return 0
            def terminate(self):
                self.returncode = -15
                self._done.set()

        orig_exec = asyncio.create_subprocess_exec
        captured_env = []

        async def fake_exec(*cmd, **kwargs):
            if cmd and cmd[0] == "ssh":
                captured_env.append(kwargs.get("env", {}))
                return WaitProc()
            return await orig_exec(*cmd, **kwargs)

        with patch.object(aerotunnel, "CONFIG_PATH", self.cfg_path), \
             patch.object(aerotunnel, "STABILITY_WAIT_SEC", 0.3), \
             patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            app = aerotunnel.TunnelApp()
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.pause(0.1)
                self.assertTrue(captured_env)
                env = captured_env[0]
                askpass_bin = env["SSH_ASKPASS"]

                # Spawn the real generated SSH_ASKPASS executable just like OpenSSH does
                helper_proc = await asyncio.create_subprocess_exec(
                    askpass_bin,
                    "Enter passphrase for key '/home/alice/.ssh/id_ed25519':",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                for _ in range(20):
                    await pilot.pause(0.05)
                    if isinstance(app.screen, aerotunnel.SshPasswordPrompt):
                        break
                self.assertIsInstance(app.screen, aerotunnel.SshPasswordPrompt)

                # Type password & hit enter
                await pilot.press(*list("s3cr3t"), "enter")
                out, err = await asyncio.wait_for(helper_proc.communicate(), timeout=2.0)
                self.assertEqual(helper_proc.returncode, 0, err.decode())
                self.assertEqual(out.decode().strip(), "s3cr3t")

                # Second prompt with bracketed IPv6 / Rich markup text: cancel with escape -> helper exits non-zero
                helper_proc2 = await asyncio.create_subprocess_exec(
                    askpass_bin,
                    "alice@[::1]'s password ([fingerprint]): ",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                for _ in range(20):
                    await pilot.pause(0.05)
                    if isinstance(app.screen, aerotunnel.SshPasswordPrompt):
                        break
                self.assertIsInstance(app.screen, aerotunnel.SshPasswordPrompt)
                await pilot.press("escape")
                out2, _ = await asyncio.wait_for(helper_proc2.communicate(), timeout=2.0)
                self.assertNotEqual(helper_proc2.returncode, 0)
                self.assertEqual(out2, b"")
                await pilot.pause(0.1)
                self.assertEqual(app.stats.status, "Auth Cancelled")

                # Pressing 'r' retries from Auth Cancelled and establishes a fresh attempt
                attempts_before = app.stats.attempts
                await pilot.press("r")
                await pilot.pause(0.1)
                self.assertGreater(app.stats.attempts, attempts_before)

    async def test_stability_timer_pauses_while_password_modal_open(self):
        class WaitProc:
            returncode = None
            class _Empty:
                async def read(self, n=-1):
                    await asyncio.sleep(10)
                    return b""
            def __init__(self):
                self.stdout = self._Empty()
                self.stderr = self._Empty()
                self._done = asyncio.Event()
            async def wait(self):
                await self._done.wait()
                return 0
            def terminate(self):
                self.returncode = -15
                self._done.set()

        orig_exec = asyncio.create_subprocess_exec
        captured_env = []
        async def fake_exec(*cmd, **kwargs):
            if cmd and cmd[0] == "ssh":
                captured_env.append(kwargs.get("env", {}))
                return WaitProc()
            return await orig_exec(*cmd, **kwargs)

        with patch.object(aerotunnel, "CONFIG_PATH", self.cfg_path), \
             patch.object(aerotunnel, "STABILITY_WAIT_SEC", 0.5), \
             patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            app = aerotunnel.TunnelApp()
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.pause(0.05)
                env = captured_env[0]
                helper_proc = await asyncio.create_subprocess_exec(
                    env["SSH_ASKPASS"],
                    "alice@example.com's password: ",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                for _ in range(30):
                    await pilot.pause(0.05)
                    if isinstance(app.screen, aerotunnel.SshPasswordPrompt):
                        break
                self.assertIsInstance(app.screen, aerotunnel.SshPasswordPrompt)
                # Hold modal open longer than STABILITY_WAIT_SEC (0.5s)
                await pilot.pause(0.6)
                self.assertEqual(app.stats.status, "Password Required")
                self.assertNotEqual(app.service_statuses["local_svc"], "active")

                # Submit password -> stability clock starts after prompt closes
                await pilot.press(*list("pw"), "enter")
                await asyncio.wait_for(helper_proc.communicate(), timeout=2.0)
                await pilot.pause(0.65)
                self.assertEqual(app.stats.status, "CONNECTED")
                self.assertEqual(app.service_statuses["local_svc"], "active")

    @unittest.skipUnless(
        shutil.which("ssh-keygen") and shutil.which("ssh-agent") and shutil.which("ssh-add"),
        "OpenSSH client utilities required",
    )
    async def test_real_openssh_executes_generated_askpass_helper(self):
        # Verify real OpenSSH client invokes askpass_script via SSH_ASKPASS_REQUIRE=force
        key_path = os.path.join(self.tmp.name, "test_ed25519")
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "keypass123", "-f", key_path, "-q"],
            check=True,
        )
        agent_out = subprocess.check_output(["ssh-agent", "-s"], text=True)
        agent_env = os.environ.copy()
        agent_pid = None
        for part in agent_out.split(";"):
            part = part.strip()
            if part.startswith("SSH_AUTH_SOCK="):
                agent_env["SSH_AUTH_SOCK"] = part.split("=", 1)[1]
            elif part.startswith("SSH_AGENT_PID="):
                agent_pid = int(part.split("=", 1)[1])
                agent_env["SSH_AGENT_PID"] = str(agent_pid)

        try:
            class WaitProc:
                returncode = None
                class _Empty:
                    async def read(self, n=-1):
                        await asyncio.sleep(10)
                        return b""
                def __init__(self):
                    self.stdout = self._Empty()
                    self.stderr = self._Empty()
                    self._done = asyncio.Event()
                async def wait(self):
                    await self._done.wait()
                    return 0
                def terminate(self):
                    self.returncode = -15
                    self._done.set()

            orig_exec = asyncio.create_subprocess_exec
            captured_env = []
            async def fake_exec(*cmd, **kwargs):
                if cmd and cmd[0] == "ssh":
                    captured_env.append(kwargs.get("env", {}))
                    return WaitProc()
                return await orig_exec(*cmd, **kwargs)

            with patch.object(aerotunnel, "CONFIG_PATH", self.cfg_path), \
                 patch.object(aerotunnel, "STABILITY_WAIT_SEC", 0.3), \
                 patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
                app = aerotunnel.TunnelApp()
                async with app.run_test(size=(120, 35)) as pilot:
                    await pilot.pause(0.1)
                    env = agent_env.copy()
                    env["SSH_ASKPASS"] = captured_env[0]["SSH_ASKPASS"]
                    env["SSH_ASKPASS_REQUIRE"] = "force"
                    env["DISPLAY"] = ":0"

                    proc = await asyncio.create_subprocess_exec(
                        "ssh-add",
                        key_path,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        start_new_session=True,
                        env=env,
                    )
                    for _ in range(30):
                        await pilot.pause(0.05)
                        if isinstance(app.screen, aerotunnel.SshPasswordPrompt):
                            break
                    self.assertIsInstance(app.screen, aerotunnel.SshPasswordPrompt)
                    await pilot.press(*list("keypass123"), "enter")
                    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3.0)
                    self.assertEqual(proc.returncode, 0, stderr.decode())
        finally:
            if agent_pid:
                try:
                    os.kill(agent_pid, 15)
                except OSError:
                    pass


class TestSshConfigSupport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ssh_cfg = os.path.join(self.tmp.name, "ssh_config")
        with open(self.ssh_cfg, "w") as f:
            f.write(
                "Host ssh.example.com\n"
                "    ProxyCommand /usr/local/bin/cloudflared access ssh --hostname %h\n"
                "    User user1\n"
                "    Port 2222\n"
                "    IdentityFile ~/.ssh/your_private_key\n\n"
                "Host bastion prod-api\n"
                "    HostName 10.20.30.40\n"
                "    User ops\n\n"
                "Host *.internal !bastion\n"
                "    User wilduser\n\n"
                "Host *\n"
                "    ServerAliveInterval 30\n"
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_config_allows_ssh_config_host_without_remote_user_or_port(self):
        data = {
            "remote_host": "ssh.example.com",
            "ssh_config": self.ssh_cfg,
            "services": {"ollama": {"local_port": 11434, "service_port": 11434}},
        }
        aerotunnel.validate_config(data, logger_cb=lambda _: None)

    def test_config_supports_ssh_host_and_rejects_conflicts_and_missing_file(self):
        data_alias = {
            "ssh_host": "ssh.example.com",
            "ssh_config": self.ssh_cfg,
            "services": {"ollama": {"local_port": 11434, "service_port": 11434}},
        }
        aerotunnel.validate_config(data_alias, logger_cb=lambda _: None)

        conflict = {
            "remote_host": "a.example.com",
            "ssh_host": "b.example.com",
            "services": {"ollama": {"local_port": 11434, "service_port": 11434}},
        }
        with self.assertRaises(aerotunnel.ConfigValidationError):
            aerotunnel.validate_config(conflict, logger_cb=lambda _: None)

        missing_cfg = {
            "remote_host": "ssh.example.com",
            "ssh_config": os.path.join(self.tmp.name, "missing_ssh_config"),
            "services": {"ollama": {"local_port": 11434, "service_port": 11434}},
        }
        with self.assertRaises(aerotunnel.ConfigValidationError):
            aerotunnel.validate_config(missing_cfg, logger_cb=lambda _: None)

    def test_resolve_ssh_target_reads_proxycommand_user_port_identity_and_errors(self):
        info = aerotunnel.resolve_ssh_target("ssh.example.com", ssh_config_path=self.ssh_cfg)
        self.assertTrue(info["matched_ssh_config"])
        self.assertEqual(info["user"], "user1")
        self.assertEqual(info["port"], 2222)
        self.assertEqual(info["hostname"], "ssh.example.com")
        self.assertIn("cloudflared", info["proxy_command"])
        self.assertTrue(any("your_private_key" in p for p in info["identity_files"]))

        bad_cfg = os.path.join(self.tmp.name, "bad_ssh_config")
        with open(bad_cfg, "w") as f:
            f.write("Host broken\n    BadDirectiveName 123\n")
        bad_info = aerotunnel.resolve_ssh_target("broken", ssh_config_path=bad_cfg)
        self.assertTrue(bad_info["error"])
        self.assertIn("baddirectivename", bad_info["error"].lower())

    def test_list_ssh_config_hosts_parses_concrete_entries_only(self):
        hosts = aerotunnel.list_ssh_config_hosts(self.ssh_cfg)
        aliases = [h["host"] for h in hosts]
        self.assertEqual(aliases, ["ssh.example.com", "bastion", "prod-api"])
        by_alias = {h["host"]: h for h in hosts}
        self.assertEqual(by_alias["bastion"]["hostname"], "10.20.30.40")
        self.assertEqual(by_alias["ssh.example.com"]["user"], "user1")

    def test_build_ssh_command_preserves_ssh_config_unless_overridden(self):
        cfg = {
            "ssh_host": "ssh.example.com",
            "ssh_config": self.ssh_cfg,
            "services": {"web": {"local_port": 8080, "service_port": 80}},
        }
        cmd_inherit = aerotunnel.build_ssh_tunnel_command(
            cfg, ["web"], {"web": "127.0.0.1"}
        )
        self.assertIn("-F", cmd_inherit)
        self.assertEqual(cmd_inherit[cmd_inherit.index("-F") + 1], self.ssh_cfg)
        self.assertNotIn("-p", cmd_inherit)
        self.assertEqual(cmd_inherit[-1], "ssh.example.com")

        cfg_override = {
            "remote_host": "ssh.example.com",
            "remote_user": "alice",
            "remote_port": 2200,
            "ssh_config": self.ssh_cfg,
            "services": {"web": {"local_port": 8080, "service_port": 80}},
        }
        cmd_override = aerotunnel.build_ssh_tunnel_command(
            cfg_override, ["web"], {"web": "127.0.0.1"}
        )
        self.assertEqual(cmd_override[cmd_override.index("-p") + 1], "2200")
        self.assertEqual(cmd_override[-1], "alice@ssh.example.com")

    def test_classify_ssh_failure_handles_ssh_config_proxy_and_key_errors(self):
        self.assertEqual(
            aerotunnel.classify_ssh_failure(["/home/u/.ssh/config: line 4: Bad configuration option: foo"])[0],
            "SSH Config Error",
        )
        self.assertEqual(
            aerotunnel.classify_ssh_failure(["exec: /usr/local/bin/cloudflared: No such file or directory"])[0],
            "Proxy Cmd Missing",
        )
        self.assertEqual(
            aerotunnel.classify_ssh_failure([
                "kex_exchange_identification: Connection closed by remote host",
                "Connection closed by UNKNOWN port 65535",
            ])[0],
            "Proxy Failed",
        )
        self.assertEqual(
            aerotunnel.classify_ssh_failure(["WARNING: UNPROTECTED PRIVATE KEY FILE!"])[0],
            "Key Perms Error",
        )


class TestSshConfigTuiAndProxyLoop(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ssh_cfg = os.path.join(self.tmp.name, "ssh_config")
        with open(self.ssh_cfg, "w") as f:
            f.write(
                "Host ssh.example.com\n"
                "    ProxyCommand /bin/sh -c 'exit 1'\n"
                "    User user1\n"
                "    Port 2222\n"
                "    IdentityFile ~/.ssh/your_private_key\n\n"
                "Host gpu-box\n"
                "    HostName 10.10.0.5\n"
                "    User ml\n"
                "    Port 2202\n"
            )
        self.cfg_path = os.path.join(self.tmp.name, "config.json")
        with open(self.cfg_path, "w") as f:
            json.dump(
                {
                    "remote_host": "ssh.example.com",
                    "ssh_config": self.ssh_cfg,
                    "services": {
                        "ollama": {"local_port": 11434, "service_port": 11434},
                        "rev": {"type": "remote", "remote_port": 9000, "service_port": 9000},
                    },
                },
                f,
            )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_stats_display_and_host_picker_modal(self):
        @aerotunnel.work
        async def noop_loop(self):
            return

        with patch.object(aerotunnel, "CONFIG_PATH", self.cfg_path), \
             patch.object(aerotunnel, "DEFAULT_SSH_CONFIG_PATH", self.ssh_cfg), \
             patch.object(aerotunnel.TunnelApp, "run_ssh_loop", noop_loop):
            app = aerotunnel.TunnelApp()
            async with app.run_test(size=(130, 35)) as pilot:
                await pilot.pause(0.1)
                self.assertEqual(app.resolved_ssh["user"], "user1")
                self.assertEqual(app.resolved_ssh["port"], 2222)
                self.assertIn("ProxyCommand", app.resolved_ssh["route_summary"])

                # Render stats and ensure no leading '@ssh.example.com' bug
                from rich.console import Console
                con = Console(width=80, record=True)
                con.print(app.query_one("#stats_text", aerotunnel.Static).content)
                stats_text = con.export_text()
                self.assertIn("user1@ssh.example.com", stats_text)
                self.assertIn("2222", stats_text)
                self.assertNotIn("@ssh.example.com", stats_text.replace("user1@ssh.example.com", ""))

                # Press 't' to open SshHostModal, arrow down to 'gpu-box', press Enter
                await pilot.press("t")
                await pilot.pause(0.1)
                self.assertIsInstance(app.screen, aerotunnel.SshHostModal)
                await pilot.press("down", "enter")
                await pilot.pause(0.1)
                self.assertEqual(app.config["remote_host"], "gpu-box")
                self.assertEqual(app.resolved_ssh["user"], "ml")
                self.assertEqual(app.resolved_ssh["port"], 2202)
                self.assertEqual(app.resolved_ssh["hostname"], "10.10.0.5")

    async def test_proxy_auth_url_pauses_connected_until_authenticated(self):
        class ProxyStream:
            def __init__(self, steps):
                self.steps = list(steps)
            async def read(self, n=-1):
                if self.steps:
                    delay, data = self.steps.pop(0)
                    await asyncio.sleep(delay)
                    return data
                await asyncio.sleep(10)
                return b""

        class EmptyStream:
            async def read(self, n=-1):
                await asyncio.sleep(10)
                return b""

        class ProxyProc:
            returncode = None
            def __init__(self):
                self.stdout = EmptyStream()
                self.stderr = ProxyStream([
                    (0.02, b"Please open the following URL and log in with your Cloudflare Access account:\n"),
                    (0.02, b"https://example.cloudflareaccess.com/cdn-cgi/access/cli?edge_token=abc\n"),
                    (0.35, b"Authenticated to ssh.example.com (via proxy) using \"publickey\".\n"),
                ])
                self._done = asyncio.Event()
            async def wait(self):
                await self._done.wait()
                return 0
            def terminate(self):
                self.returncode = -15
                self._done.set()

        async def fake_exec(*cmd, **kwargs):
            return ProxyProc()

        with patch.object(aerotunnel, "CONFIG_PATH", self.cfg_path), \
             patch.object(aerotunnel, "STABILITY_WAIT_SEC", 0.2), \
             patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            app = aerotunnel.TunnelApp()
            async with app.run_test(size=(120, 35)) as pilot:
                # At t=0.28s (> STABILITY_WAIT_SEC 0.2s), proxy auth is still waiting -> NOT CONNECTED!
                await pilot.pause(0.28)
                self.assertEqual(app.stats.status, "Proxy Auth")
                self.assertNotEqual(app.service_statuses["ollama"], "active")

                # Once Authenticated line arrives + stability window elapses -> CONNECTED!
                await pilot.pause(0.45)
                self.assertEqual(app.stats.status, "CONNECTED")
                self.assertEqual(app.service_statuses["ollama"], "active")
                self.assertEqual(app.service_statuses["rev"], "active")

    async def test_real_openssh_fails_cleanly_on_missing_proxycommand_executable(self):
        bad_proxy_ssh = os.path.join(self.tmp.name, "bad_proxy_ssh")
        missing_bin = os.path.join(self.tmp.name, "no_such_cloudflared")
        with open(bad_proxy_ssh, "w") as f:
            f.write(
                "Host ssh.example.com\n"
                f"    ProxyCommand {missing_bin} access ssh --hostname %h\n"
                "    User user1\n"
            )
        with open(self.cfg_path, "w") as f:
            json.dump(
                {
                    "remote_host": "ssh.example.com",
                    "ssh_config": bad_proxy_ssh,
                    "services": {"ollama": {"local_port": 11434, "service_port": 11434}},
                },
                f,
            )

        with patch.object(aerotunnel, "CONFIG_PATH", self.cfg_path), \
             patch.object(aerotunnel, "RECONNECT_DELAY_SEC", 5.0):
            app = aerotunnel.TunnelApp()
            async with app.run_test(size=(120, 35)) as pilot:
                for _ in range(30):
                    await pilot.pause(0.05)
                    if app.stats.status == "Proxy Cmd Missing":
                        break
                self.assertEqual(app.stats.status, "Proxy Cmd Missing")


if __name__ == "__main__":
    unittest.main()
