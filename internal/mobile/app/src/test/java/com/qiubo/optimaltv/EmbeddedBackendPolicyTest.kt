package com.qiubo.optimaltv

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class EmbeddedBackendPolicyTest {
    @get:Rule val tmp = TemporaryFolder()

    @Test fun `identical signature reuses the extracted directory`() {
        assertTrue(EmbeddedBackendPolicy.canReuse("assets|110|40992121", "assets|110|40992121"))
    }

    @Test fun `missing marker never reuses the directory`() {
        assertFalse(EmbeddedBackendPolicy.canReuse(null, "assets|110|40992121"))
        assertFalse(EmbeddedBackendPolicy.canReuse("", "assets|110|40992121"))
    }

    @Test fun `hot update signature does not satisfy an assets signature`() {
        assertFalse(EmbeddedBackendPolicy.canReuse("hot|30|40972839", "assets|110|40992121"))
    }

    /** 回归：热更包被隔离后，标记必须消失，否则回退 assets 会复用坏包目录。 */
    @Test fun `quarantine invalidates the extraction marker`() {
        val dir = tmp.newFolder("embedded_backend")
        File(dir, "media_index.py").writeText("old backend")
        val flag = File(dir, EmbeddedBackendPolicy.MARKER_NAME)
        flag.writeText("hot|30|40972839")

        assertTrue(EmbeddedBackendPolicy.invalidateAfterQuarantine(dir))
        assertFalse(flag.exists())
        assertFalse(File(dir, "media_index.py").exists())
    }

    @Test fun `quarantine on a clean directory reports no change`() {
        val dir = tmp.newFolder("embedded_backend_clean")
        assertFalse(EmbeddedBackendPolicy.invalidateAfterQuarantine(dir))
    }
}
