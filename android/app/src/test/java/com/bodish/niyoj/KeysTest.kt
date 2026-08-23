package com.bodish.niyoj

import com.hierynomus.sshj.userauth.keyprovider.OpenSSHKeyV1KeyFile
import net.schmizz.sshj.common.Buffer
import java.util.Base64
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * The only thing in the Android app that hand-rolls a binary format. If the
 * openssh-key-v1 writer drifts, sshj stops loading the key and every deploy
 * fails with an unhelpful auth error — so prove sshj can read what we wrote.
 */
class KeysTest {

    @Test
    fun `generated key round-trips through sshj`() {
        val priv = File.createTempFile("niyoj", "").apply { delete() }
        try {
            val line = Keys.generate(priv, "niyoj-test")
            assertTrue(line.startsWith("ssh-ed25519 "))
            assertEquals(line, Keys.readPub(priv))

            val loaded = OpenSSHKeyV1KeyFile().apply { init(priv) }
            val blob = Buffer.PlainBuffer().putPublicKey(loaded.public).compactData
            assertEquals(line.split(" ")[1], Base64.getEncoder().encodeToString(blob))
        } finally {
            priv.delete()
            File(priv.path + ".pub").delete()
        }
    }

    @Test
    fun `an existing key is never overwritten`() {
        val priv = File.createTempFile("niyoj", "").apply { delete() }
        try {
            val first = Keys.generate(priv, "niyoj-test")
            val body = priv.readText()
            assertEquals(first, Keys.generate(priv, "niyoj-test"))
            assertEquals(body, priv.readText())
        } finally {
            priv.delete()
            File(priv.path + ".pub").delete()
        }
    }
}
