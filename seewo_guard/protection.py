# -*- coding: utf-8 -*-
"""
protection.py - 进程保护层 (GUI 与守护进程共用)
Layer 1: 高优先级
Layer 2: 特权提升
Layer 3: 缓解策略 (DEP/ASLR/严格句柄/CFG)
Layer 4: PPL 尝试 (失败不影响运行)
"""
import ctypes
import logging

from seewo_guard.win_api import (
    kernel32, advapi32,
    SetPriorityClass, GetPriorityClass, HIGH_PRIORITY_CLASS,
    SetProcessMitigationPolicy,
    ProcessDEPPolicy, ProcessASLRPolicy,
    ProcessStrictHandleCheckPolicy, ProcessControlFlowGuardPolicy,
    NtSetInformationProcess, ProcessProtectionInformation,
    PsProtectedTypeProtectedLight,
    PsProtectedSignerAntimalware, PsProtectedSignerLsa,
    PsProtectedSignerWindows, PsProtectedSignerWinTcb,
    PsProtectedSignerAuthenticode, PsProtectedSignerCodeGen,
    OpenProcessToken, LookupPrivilegeValueW, AdjustTokenPrivileges,
    TOKEN_ADJUST_PRIVILEGES, TOKEN_QUERY, SE_PRIVILEGE_ENABLED,
    LUID, TOKEN_PRIVILEGES, CloseHandle,
    wintypes,
)
from ctypes import wintypes as _wintypes


class ProcessProtection:
    """进程保护管理器 (优先级/特权/缓解/PPL)"""

    def __init__(self):
        self._privileges_granted = []
        self._original_priority = None
        self._ppl_set = False
        self._ppl_signer = None
        self._active = False
        self._cleaned = False

    # ---------- 启用 ----------
    def enable(self):
        if self._active:
            return
        logging.info("🛡️ 正在启用进程保护...")
        self._set_priority(HIGH_PRIORITY_CLASS)
        self._enable_privileges()
        self._set_mitigation_policies()
        self._try_enable_ppl()
        self._active = True
        logging.info("✅ 进程保护已启用 (优先级/特权/缓解/PPL)")

    # ---------- 解除 ----------
    def disable(self):
        if self._cleaned:
            return
        self._active = False
        self._try_disable_ppl()
        self._disable_privileges()
        self._restore_priority()
        self._cleaned = True
        logging.info("🔓 进程保护已解除 (安全)")

    def is_active(self):
        return self._active

    # ---------- Layer 1: 优先级 ----------
    def _set_priority(self, priority_class):
        try:
            h = kernel32.GetCurrentProcess()
            self._original_priority = GetPriorityClass(h)
            if SetPriorityClass(h, priority_class):
                logging.info(f"  ✓ 优先级已提升 (0x{priority_class:08X})")
            else:
                logging.warning("  ✗ 优先级提升失败")
        except Exception as e:
            logging.error(f"  ✗ 优先级设置异常: {e}")

    def _restore_priority(self):
        if not self._original_priority:
            return
        try:
            SetPriorityClass(kernel32.GetCurrentProcess(), self._original_priority)
        except Exception as e:
            logging.error(f"  ✗ 优先级恢复异常: {e}")

    # ---------- Layer 2: 特权 ----------
    def _enable_privileges(self):
        privileges = [
            ("SeDebugPrivilege", "调试权限"),
            ("SeShutdownPrivilege", "关机权限"),
            ("SeIncreaseBasePriorityPrivilege", "提升基优先级"),
            ("SeTcbPrivilege", "TCB权限"),
            ("SeSecurityPrivilege", "安全权限"),
            ("SeLoadDriverPrivilege", "加载驱动"),
        ]
        try:
            h_token = _wintypes.HANDLE()
            if not OpenProcessToken(kernel32.GetCurrentProcess(),
                                    TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                    ctypes.byref(h_token)):
                return
            for name, desc in privileges:
                try:
                    luid = LUID()
                    if not LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
                        continue
                    tp = TOKEN_PRIVILEGES()
                    tp.PrivilegeCount = 1
                    tp.Privileges[0].Luid = luid
                    tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
                    if AdjustTokenPrivileges(h_token, False, ctypes.byref(tp),
                                             0, None, None):
                        if ctypes.get_last_error() == 0:
                            self._privileges_granted.append(name)
                            logging.info(f"  ✓ {desc} 已启用")
                except Exception:
                    continue
            CloseHandle(h_token)
        except Exception as e:
            logging.error(f"  ✗ 特权提升异常: {e}")

    def _disable_privileges(self):
        if not self._privileges_granted:
            return
        try:
            h_token = _wintypes.HANDLE()
            if not OpenProcessToken(kernel32.GetCurrentProcess(),
                                    TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                    ctypes.byref(h_token)):
                return
            for name in self._privileges_granted:
                try:
                    luid = LUID()
                    if not LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
                        continue
                    tp = TOKEN_PRIVILEGES()
                    tp.PrivilegeCount = 1
                    tp.Privileges[0].Luid = luid
                    tp.Privileges[0].Attributes = 0
                    AdjustTokenPrivileges(h_token, False, ctypes.byref(tp),
                                          0, None, None)
                except Exception:
                    continue
            CloseHandle(h_token)
            logging.info(f"  ✓ 已撤销 {len(self._privileges_granted)} 项特权")
            self._privileges_granted.clear()
        except Exception:
            pass

    # ---------- Layer 3: 缓解策略 ----------
    def _set_mitigation_policies(self):
        class DWORD_POLICY(ctypes.Structure):
            _fields_ = [("Flags", _wintypes.DWORD)]

        class DEP_POLICY(ctypes.Structure):
            _fields_ = [("Flags", _wintypes.DWORD), ("Permanent", _wintypes.DWORD)]

        results = []

        def _apply(policy_id, struct, name):
            try:
                if SetProcessMitigationPolicy(policy_id, ctypes.byref(struct),
                                              ctypes.sizeof(struct)):
                    results.append(name + "✓")
                else:
                    results.append(name + "✗")
            except Exception:
                results.append(name + "✗")

        dep = DEP_POLICY()
        dep.Flags = 0x00000001
        _apply(ProcessDEPPolicy, dep, "DEP")

        aslr = DWORD_POLICY()
        aslr.Flags = 0x00000007
        _apply(ProcessASLRPolicy, aslr, "ASLR")

        sh = DWORD_POLICY()
        sh.Flags = 0x00000001
        _apply(ProcessStrictHandleCheckPolicy, sh, "StrictHandle")

        cfg = DWORD_POLICY()
        cfg.Flags = 0x00000001
        _apply(ProcessControlFlowGuardPolicy, cfg, "CFG")

        logging.info(f"  缓解策略: {', '.join(results)}")

    # ---------- Layer 4: PPL ----------
    def _try_enable_ppl(self):
        try:
            class PS_PROTECTION(ctypes.Structure):
                _fields_ = [("Level", ctypes.c_ubyte)]

                def set_protection(self, ptype, signer, audit=0):
                    self.Level = ((ptype & 0x07) | ((audit & 0x01) << 3)
                                  | ((signer & 0x0F) << 4))

            signers = [
                (PsProtectedSignerAntimalware, "Antimalware"),
                (PsProtectedSignerLsa, "Lsa"),
                (PsProtectedSignerWindows, "Windows"),
                (PsProtectedSignerWinTcb, "WinTcb"),
                (PsProtectedSignerAuthenticode, "Authenticode"),
                (PsProtectedSignerCodeGen, "CodeGen"),
            ]
            h = kernel32.GetCurrentProcess()
            for signer, name in signers:
                try:
                    prot = PS_PROTECTION()
                    prot.set_protection(PsProtectedTypeProtectedLight, signer)
                    status = NtSetInformationProcess(
                        h, ProcessProtectionInformation,
                        ctypes.byref(prot), ctypes.sizeof(prot))
                    if status == 0:
                        self._ppl_set = True
                        self._ppl_signer = name
                        logging.info(f"  ✓ PPL 已启用 ({name} signer)")
                        return True
                except Exception:
                    continue
            logging.info("  ℹ️ PPL 未启用 (需微软签名证书, 属正常现象)")
            return False
        except Exception as e:
            logging.debug(f"  - PPL 异常: {e}")
            return False

    def _try_disable_ppl(self):
        if not self._ppl_set:
            return
        try:
            class PS_PROTECTION(ctypes.Structure):
                _fields_ = [("Level", ctypes.c_ubyte)]

            prot = PS_PROTECTION()
            prot.Level = 0x00
            status = NtSetInformationProcess(
                kernel32.GetCurrentProcess(), ProcessProtectionInformation,
                ctypes.byref(prot), ctypes.sizeof(prot))
            if status == 0:
                self._ppl_set = False
                self._ppl_signer = None
                logging.info("  ✓ PPL 已清除")
            else:
                logging.info("  ℹ️ PPL 无法降级 (进程退出自动清除)")
        except Exception:
            pass

    def is_ppl_enabled(self):
        return self._ppl_set


_protection = None


def get_protection():
    global _protection
    if _protection is None:
        _protection = ProcessProtection()
    return _protection
