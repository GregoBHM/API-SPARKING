import java.nio.charset.StandardCharsets;
import java.security.KeyFactory;
import java.security.PublicKey;
import java.security.Signature;
import java.security.spec.X509EncodedKeySpec;
import java.util.Base64;

/**
 * Java 8-compatible verifier for Sparking License API signed responses.
 *
 * IMPORTANT: embed/pin the public key obtained from YOUR own deployment.
 * Do not download and blindly trust the public key on every startup.
 */
public final class SignedLicenseVerifier {

    private SignedLicenseVerifier() {}

    public static boolean verify(String token, String signatureB64Url, String publicKeyPem) throws Exception {
        PublicKey publicKey = parsePublicKey(publicKeyPem);
        Signature verifier = Signature.getInstance("SHA256withRSA");
        verifier.initVerify(publicKey);
        verifier.update(token.getBytes(StandardCharsets.US_ASCII));
        return verifier.verify(decodeBase64Url(signatureB64Url));
    }

    public static String decodePayloadJson(String token) {
        byte[] decoded = decodeBase64Url(token);
        return new String(decoded, StandardCharsets.UTF_8);
    }

    private static PublicKey parsePublicKey(String pem) throws Exception {
        String clean = pem
                .replace("-----BEGIN PUBLIC KEY-----", "")
                .replace("-----END PUBLIC KEY-----", "")
                .replaceAll("\\s", "");
        byte[] bytes = Base64.getDecoder().decode(clean);
        return KeyFactory.getInstance("RSA").generatePublic(new X509EncodedKeySpec(bytes));
    }

    private static byte[] decodeBase64Url(String value) {
        int padding = (4 - value.length() % 4) % 4;
        StringBuilder padded = new StringBuilder(value);
        for (int i = 0; i < padding; i++) padded.append('=');
        return Base64.getUrlDecoder().decode(padded.toString());
    }
}
