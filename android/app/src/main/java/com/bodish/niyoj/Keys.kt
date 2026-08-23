package com.bodish.niyoj

import org.bouncycastle.crypto.generators.Ed25519KeyPairGenerator
import org.bouncycastle.crypto.params.Ed25519KeyGenerationParameters
import org.bouncycastle.crypto.params.Ed25519PrivateKeyParameters
import org.bouncycastle.crypto.params.Ed25519PublicKeyParameters
import java.io.ByteArrayOutputStream
import java.io.File
import java.security.SecureRandom
import java.util.Base64

/**
 * ed25519 keys in the exact files ssh-keygen would write, so a key made on the
 * phone can be copied to a laptop and vice versa.
 */
object Keys {

    private fun ByteArrayOutputStream.u32(v: Int) = write(
        byteArrayOf((v ushr 24).toByte(), (v ushr 16).toByte(), (v ushr 8).toByte(), v.toByte())
    )

    private fun ByteArrayOutputStream.str(b: ByteArray) {
        u32(b.size); write(b)
    }

    private fun ByteArrayOutputStream.str(s: String) = str(s.toByteArray())

    fun pubBlob(pub: ByteArray): ByteArray =
        ByteArrayOutputStream().apply { str("ssh-ed25519"); str(pub) }.toByteArray()

    fun pubLine(pub: ByteArray, comment: String): String =
        "ssh-ed25519 " + Base64.getEncoder().encodeToString(pubBlob(pub)) + " " + comment

    /** openssh-key-v1, unencrypted — a deploy must never stop to ask for a passphrase. */
    fun privPem(seed: ByteArray, pub: ByteArray, comment: String): String {
        val check = SecureRandom().nextInt()
        val inner = ByteArrayOutputStream().apply {
            u32(check); u32(check)                 // both must match once decrypted
            str("ssh-ed25519"); str(pub); str(seed + pub); str(comment)
            var pad = 1
            while (size() % 8 != 0) write(pad++)   // pad to the "none" cipher block size
        }.toByteArray()
        val body = ByteArrayOutputStream().apply {
            write("openssh-key-v1".toByteArray()); write(0)
            str("none"); str("none"); str(ByteArray(0))
            u32(1); str(pubBlob(pub)); str(inner)
        }.toByteArray()
        val b64 = Base64.getEncoder().encodeToString(body).chunked(70).joinToString("\n")
        return "-----BEGIN OPENSSH PRIVATE KEY-----\n$b64\n-----END OPENSSH PRIVATE KEY-----\n"
    }

    /** Returns the public key line. Never overwrites: losing a private key locks
     *  you out of every server that trusts it. */
    fun generate(priv: File, comment: String): String {
        readPub(priv).let { if (priv.exists() && it.isNotEmpty()) return it }
        val gen = Ed25519KeyPairGenerator().apply {
            init(Ed25519KeyGenerationParameters(SecureRandom()))
        }
        val pair = gen.generateKeyPair()
        val seed = (pair.private as Ed25519PrivateKeyParameters).encoded
        val pub = (pair.public as Ed25519PublicKeyParameters).encoded
        priv.parentFile?.mkdirs()
        priv.writeText(privPem(seed, pub, comment))
        priv.setReadable(false, false)
        priv.setReadable(true, true)
        val line = pubLine(pub, comment)
        File(priv.path + ".pub").writeText(line + "\n")
        return line
    }

    fun readPub(priv: File): String =
        File(priv.path + ".pub").takeIf { it.exists() }?.readText()?.trim().orEmpty()
}
