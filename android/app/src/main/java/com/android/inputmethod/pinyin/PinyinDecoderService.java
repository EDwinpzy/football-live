/*
 * AOSP 谷歌拼音解码器 JNI 包装（Apache-2.0，源码来自 AOSP
 * packages/inputmethods/PinyinIME，2026-08-29 内嵌本项目）。
 *
 * 类名/包名不可改：native 层 JNI_OnLoad 用 RegisterNatives 绑定到
 * "com/android/inputmethod/pinyin/PinyinDecoderService"。原版的 IME Service
 * 外壳（AIDL/进程隔离）已剥掉，只保留解码内核 + 精简静态门面。
 */
package com.android.inputmethod.pinyin;

import android.content.Context;
import android.util.Log;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.List;

public class PinyinDecoderService {
    private static final String TAG = "PinyinDecoderService";

    native static boolean nativeImOpenDecoder(byte fn_sys_dict[], byte fn_usr_dict[]);

    native static boolean nativeImOpenDecoderFd(java.io.FileDescriptor fd,
            long startOffset, long length, byte fn_usr_dict[]);

    native static void nativeImSetMaxLens(int maxSpsLen, int maxHzsLen);

    native static boolean nativeImCloseDecoder();

    native static int nativeImSearch(byte pyBuf[], int pyLen);

    native static int nativeImDelSearch(int pos, boolean is_pos_in_splid,
            boolean clear_fixed_this_step);

    native static void nativeImResetSearch();

    native static int nativeImAddLetter(byte ch);

    native static String nativeImGetPyStr(boolean decoded);

    native static int nativeImGetPyStrLen(boolean decoded);

    native static int[] nativeImGetSplStart();

    native static String nativeImGetChoice(int choiceId);

    native static int nativeImChoose(int choiceId);

    native static int nativeImCancelLastChoice();

    native static int nativeImGetFixedLen();

    native static boolean nativeImCancelInput();

    native static boolean nativeImFlushCache();

    native static int nativeImGetPredictsNum(String fixedStr);

    native static String nativeImGetPredictItem(int predictNo);

    native static String nativeSyncUserDict(byte[] user_dict, String tomerge);

    native static boolean nativeSyncBegin(byte[] user_dict);

    native static boolean nativeSyncFinish();

    native static String nativeSyncGetLemmas();

    native static int nativeSyncPutLemmas(String tomerge);

    native static int nativeSyncGetLastCount();

    native static int nativeSyncGetTotalCount();

    native static boolean nativeSyncClearLastGot();

    native static int nativeSyncGetCapacity();

    private static final int MAX_PATH_FILE_LENGTH = 100;
    /**
     * 续写预测时喂给引擎的历史上限，与 native 的 kMaxPredictSize=7 保持一致。
     */
    private static final int MAX_PREDICT_HISTORY = 7;
    private static volatile boolean inited = false;
    private static volatile boolean loadFailed = false;
    private static final Object LOCK = new Object();

    static {
        try {
            System.loadLibrary("jni_pinyinime");
        } catch (UnsatisfiedLinkError ule) {
            loadFailed = true;
            Log.e(TAG, "Could not load jni_pinyinime: " + ule.getMessage());
        }
    }

    /** 解码器是否可用（so 加载成功且词典已打开） */
    public static boolean isReady() {
        return inited;
    }

    /** so 是否加载失败（调用方据此回退纯 Kotlin 词典引擎） */
    public static boolean isLoadFailed() {
        return loadFailed;
    }

    /**
     * 初始化：把 assets 内置系统词典解出到应用私有目录（native 需要真实文件路径），
     * 用户词典文件不存在时由 native 自动创建。幂等，成功过则直接返回。
     */
    public static boolean init(Context ctx) {
        if (inited || loadFailed) return inited;
        synchronized (LOCK) {
            if (inited || loadFailed) return inited;
            try {
                File sysDict = new File(ctx.getFilesDir(), "pinyin_sys_dict.dat");
                // 词典升级检测：按 assets 内词典字节长度对比（zip 升级后旧文件必须重解）
                long assetLen;
                try (InputStream in = ctx.getAssets().open("pinyin_dict_pinyin.dat")) {
                    assetLen = in.available();
                }
                if (!sysDict.exists() || sysDict.length() != assetLen) {
                    try (InputStream in = ctx.getAssets().open("pinyin_dict_pinyin.dat");
                         OutputStream out = new FileOutputStream(sysDict)) {
                        byte[] buf = new byte[65536];
                        int n;
                        while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                    }
                }
                byte[] usr = new byte[MAX_PATH_FILE_LENGTH];
                String usrPath = new File(ctx.getFilesDir(), "pinyin_usr_dict.dat").getAbsolutePath();
                if (usrPath.length() >= MAX_PATH_FILE_LENGTH) {
                    Log.e(TAG, "usr dict path too long");
                    return false;
                }
                for (int i = 0; i < usrPath.length(); i++) usr[i] = (byte) usrPath.charAt(i);
                usr[usrPath.length()] = 0;
                byte[] sys = new byte[MAX_PATH_FILE_LENGTH];
                String sysPath = sysDict.getAbsolutePath();
                for (int i = 0; i < sysPath.length(); i++) sys[i] = (byte) sysPath.charAt(i);
                sys[sysPath.length()] = 0;
                inited = nativeImOpenDecoder(sys, usr);
                if (inited) {
                    // 搜索框场景缓冲给足：最长拼音串 / 最长汉字组合
                    nativeImSetMaxLens(64, 64);
                } else {
                    Log.e(TAG, "imOpenDecoder failed");
                }
            } catch (Exception e) {
                Log.e(TAG, "init failed: " + e.getMessage());
            }
            return inited;
        }
    }

    // ---- 解码内核门面（解码器为进程级单例，全部串行化） ----

    /** 全新搜索一个拼音串（ASCII 小写），返回候选总数 */
    public static int search(String pinyin) {
        if (!inited) return 0;
        synchronized (LOCK) {
            byte[] buf = new byte[pinyin.length() + 1];
            for (int i = 0; i < pinyin.length(); i++) buf[i] = (byte) pinyin.charAt(i);
            buf[pinyin.length()] = 0;
            return nativeImSearch(buf, buf.length - 1);
        }
    }

    /** 取第 choiceId 个候选（0 起） */
    public static String getChoice(int choiceId) {
        if (!inited) return null;
        synchronized (LOCK) {
            return nativeImGetChoice(choiceId);
        }
    }

    /** 音节切分信息：[0]=元素数，之后成对 (起始汉字下标, 拼音串起始下标) */
    public static int[] getSplStart() {
        if (!inited) return null;
        synchronized (LOCK) {
            return nativeImGetSplStart();
        }
    }

    /** 拼音串显示（decoded=true 返回引擎已确认的切分形式，如 "ai qing gong yu"） */
    public static String getPyStr(boolean decoded) {
        if (!inited) return null;
        synchronized (LOCK) {
            return nativeImGetPyStr(decoded);
        }
    }

    /**
     * 取「整词续写」候选：把已上屏的 fixedStr 交给引擎预测，取回后续字。
     *
     * native 侧 predicts 缓冲区是进程级全局（predict_buf / predict_len），必须
     * 「取总数 + 读全部条目」在同一把锁里完成，否则并发读会读到别人那一轮的结果。
     *
     * 返回的是**续写后缀**（fixedStr="爱情" → ["公寓", ...]），不是整词，
     * 调用方直接追加到已上屏文本后面即可。
     *
     * 另：native 预测缓冲按 kMaxPredictSize(7) 设计，历史只喂尾部 7 字。
     */
    public static List<String> getPredicts(String fixedStr, int limit) {
        if (!inited || fixedStr == null || fixedStr.isEmpty() || limit <= 0) {
            return new ArrayList<>(0);
        }
        synchronized (LOCK) {
            // 只取尾部 MAX_HISTORY 字喂给引擎：native 预测缓冲按 7 字设计
            String history = fixedStr.length() > MAX_PREDICT_HISTORY
                    ? fixedStr.substring(fixedStr.length() - MAX_PREDICT_HISTORY)
                    : fixedStr;
            int n = nativeImGetPredictsNum(history);
            if (n <= 0) return new ArrayList<>(0);
            List<String> out = new ArrayList<>(Math.min(n, limit));
            for (int i = 0; i < n && out.size() < limit; i++) {
                String item = nativeImGetPredictItem(i);
                if (item == null) break;
                item = item.trim();
                // 只丢空白项：续写就是追加，「人」续「人」得到「人人」是合法叠字
                if (!item.isEmpty()) out.add(item);
            }
            return out;
        }
    }
}
