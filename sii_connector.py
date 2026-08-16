from __future__ import annotations

import base64
import html
import re
import time
from pathlib import Path

import requests
from lxml import etree
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
import xmlsec


class SIIError(RuntimeError):
    pass


class SIIConnector:
    SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
    SOAP_ENC = "http://schemas.xmlsoap.org/soap/encoding/"
    XSD_NS = "http://www.w3.org/2001/XMLSchema"
    XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
    DSIG_NS = "http://www.w3.org/2000/09/xmldsig#"

    TEMPLATE = """<?xml version="1.0"?>
<getToken>
  <item>
    <Semilla>{seed}</Semilla>
  </item>
  <Signature xmlns="http://www.w3.org/2000/09/xmldsig#">
    <SignedInfo>
      <CanonicalizationMethod Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315"/>
      <SignatureMethod Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1"/>
      <Reference URI="">
        <Transforms>
          <Transform Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature"/>
        </Transforms>
        <DigestMethod Algorithm="http://www.w3.org/2000/09/xmldsig#sha1"/>
        <DigestValue/>
      </Reference>
    </SignedInfo>
    <SignatureValue/>
    <KeyInfo>
      <KeyValue>
        <RSAKeyValue>
          <Modulus>{modulus}</Modulus>
          <Exponent>{exponent}</Exponent>
        </RSAKeyValue>
      </KeyValue>
      <X509Data>
        <X509Certificate>{certificate_b64}</X509Certificate>
      </X509Data>
    </KeyInfo>
  </Signature>
</getToken>"""

    def __init__(self, p12_path, p12_password, environment="production", timeout=30):
        self.path = Path(p12_path)
        self.password = p12_password.encode("utf-8") if p12_password else None
        self.environment = environment
        self.timeout = timeout

        if environment == "production":
            self.host = "palena.sii.cl"
        elif environment == "certification":
            self.host = "maullin.sii.cl"
        else:
            raise SIIError("Ambiente inválido.")

        self.seed_url = f"https://{self.host}/DTEWS/CrSeed.jws"
        self.token_url = f"https://{self.host}/DTEWS/GetTokenFromSeed.jws"

        raw = self.path.read_bytes()
        self.private_key, self.certificate, self.extra = pkcs12.load_key_and_certificates(
            raw, self.password
        )

        if self.private_key is None or self.certificate is None:
            raise SIIError("El P12/PFX no contiene llave privada y certificado.")

        self.key_pem = self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        self.cert_pem = self.certificate.public_bytes(
            serialization.Encoding.PEM
        )

        public_numbers = self.private_key.public_key().public_numbers()
        modulus_bytes = public_numbers.n.to_bytes(
            (public_numbers.n.bit_length() + 7) // 8,
            "big",
        )
        exponent_bytes = public_numbers.e.to_bytes(
            (public_numbers.e.bit_length() + 7) // 8,
            "big",
        )
        self.modulus_b64 = base64.b64encode(modulus_bytes).decode("ascii")
        self.exponent_b64 = base64.b64encode(exponent_bytes).decode("ascii")

        cert_der = self.certificate.public_bytes(serialization.Encoding.DER)
        self.certificate_b64 = base64.b64encode(cert_der).decode("ascii")

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "SIIConnector/0.4.5",
            "Accept": "text/xml, */*",
            "Connection": "close",
        })

    def certificate_subject(self):
        return self.certificate.subject.rfc4514_string()

    def _post_soap(self, url, body, filename):
        Path(filename).write_bytes(body)

        for attempt in range(2):
            response = self.session.post(
                url,
                data=body,
                headers={
                    "Content-Type": "text/xml; charset=UTF-8",
                    "SOAPAction": '""',
                },
                timeout=self.timeout,
            )

            if response.status_code == 503:
                if attempt == 0:
                    time.sleep(2)
                    continue
                raise SIIError(f"HTTP 503 Service Unavailable desde {url}")

            response.raise_for_status()
            return response.content

        raise SIIError("No se pudo completar la llamada SOAP.")

    @staticmethod
    def _soap_string(xml_bytes, name):
        root = etree.fromstring(xml_bytes)
        vals = root.xpath(f"//*[local-name()='{name}']/text()")
        if not vals:
            raise SIIError(f"No se encontró {name} en la respuesta SOAP.")
        return html.unescape(vals[0])

    def get_seed(self):
        ns = f"https://{self.host}/DTEWS/CrSeed.jws"

        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<SOAP-ENV:Envelope xmlns:SOAP-ENV="{self.SOAP_NS}" '
            f'xmlns:SOAP-ENC="{self.SOAP_ENC}" '
            f'xmlns:xsi="{self.XSI_NS}" '
            f'xmlns:xsd="{self.XSD_NS}" '
            f'SOAP-ENV:encodingStyle="{self.SOAP_ENC}">'
            '<SOAP-ENV:Body>'
            f'<m:getSeed xmlns:m="{ns}"/>'
            '</SOAP-ENV:Body>'
            '</SOAP-ENV:Envelope>'
        ).encode("utf-8")

        response = self._post_soap(
            self.seed_url,
            body,
            "last_seed_request_v4_5.xml",
        )
        Path("last_seed_soap_response_v4_5.xml").write_bytes(response)

        inner = self._soap_string(response, "getSeedReturn")
        Path("last_seed_response_v4_5.xml").write_text(
            inner, encoding="utf-8"
        )

        root = etree.fromstring(inner.encode("utf-8"))
        estado = root.xpath("string(//*[local-name()='ESTADO'])")
        semilla = (
            root.xpath("string(//*[local-name()='SEMILLA'])")
            or root.xpath("string(//*[local-name()='SEED'])")
        )
        glosa = root.xpath("string(//*[local-name()='GLOSA'])")

        if estado != "00" or not semilla:
            raise SIIError(
                f"SII no entregó semilla. Estado {estado}: {glosa}"
            )

        return semilla.strip()

    def _make_xmlsec_key(self):
        key = xmlsec.Key.from_memory(
            self.key_pem,
            xmlsec.constants.KeyDataFormatPem,
            None,
        )
        key.load_cert_from_memory(
            self.cert_pem,
            xmlsec.constants.KeyDataFormatCertPem,
        )
        return key

    @staticmethod
    def _has_whitespace(value: str) -> bool:
        return bool(re.search(r"\s", value or ""))

    def sign_seed(self, seed):
        if self._has_whitespace(self.certificate_b64):
            raise SIIError(
                "X509Certificate Base64 contiene espacios o saltos antes de firmar."
            )

        template = self.TEMPLATE.format(
            seed=seed,
            modulus=self.modulus_b64,
            exponent=self.exponent_b64,
            certificate_b64=self.certificate_b64,
        )

        parser = etree.XMLParser(
            remove_blank_text=True,
            resolve_entities=False,
        )
        root = etree.fromstring(
            template.encode("utf-8"),
            parser=parser,
        )

        cert_nodes = root.xpath(
            "//*[local-name()='X509Certificate' and namespace-uri()=$ns]",
            ns=self.DSIG_NS,
        )
        if len(cert_nodes) != 1:
            raise SIIError("No existe exactamente un X509Certificate antes de firmar.")

        cert_text = (cert_nodes[0].text or "").strip()
        if not cert_text:
            raise SIIError("X509Certificate está vacío antes de firmar.")
        if self._has_whitespace(cert_text):
            raise SIIError(
                "X509Certificate contiene espacios o saltos antes de firmar."
            )

        signature_node = xmlsec.tree.find_node(
            root,
            xmlsec.constants.NodeSignature,
            xmlsec.constants.DSigNs,
        )
        if signature_node is None:
            raise SIIError("xmlsec no encontró Signature.")

        ctx = xmlsec.SignatureContext()
        ctx.key = self._make_xmlsec_key()
        ctx.sign(signature_node)

        signed_xml = etree.tostring(
            root,
            xml_declaration=True,
            encoding="ISO-8859-1",
            pretty_print=False,
        )

        Path("signed_seed_v4_5.xml").write_bytes(signed_xml)

        parsed = etree.fromstring(signed_xml)

        mod_nodes = parsed.xpath("//*[local-name()='Modulus']")
        exp_nodes = parsed.xpath("//*[local-name()='Exponent']")
        cert_nodes = parsed.xpath("//*[local-name()='X509Certificate']")
        transform_nodes = parsed.xpath("//*[local-name()='Transform']")

        if len(mod_nodes) != 1 or not (mod_nodes[0].text or "").strip():
            raise SIIError("Modulus vacío después de firmar.")
        if len(exp_nodes) != 1 or not (exp_nodes[0].text or "").strip():
            raise SIIError("Exponent vacío después de firmar.")
        if len(cert_nodes) != 1:
            raise SIIError("No existe exactamente un X509Certificate después de firmar.")
        if len(transform_nodes) != 1:
            raise SIIError(
                f"Se generaron {len(transform_nodes)} Transform; SII espera 1."
            )

        cert_text = (cert_nodes[0].text or "").strip()
        if not cert_text:
            raise SIIError("X509Certificate vacío después de firmar.")
        if self._has_whitespace(cert_text):
            raise SIIError(
                "X509Certificate contiene espacios o saltos después de firmar. "
                "No se enviará al SII."
            )

        return signed_xml

    def verify_signed_seed_locally(self, signed_xml):
        parser = etree.XMLParser(
            remove_blank_text=True,
            resolve_entities=False,
        )
        root = etree.fromstring(signed_xml, parser=parser)

        cert_nodes = root.xpath(
            "//*[local-name()='X509Certificate' and namespace-uri()=$ns]",
            ns=self.DSIG_NS,
        )
        if len(cert_nodes) != 1:
            raise SIIError("No existe exactamente un X509Certificate al verificar.")

        cert_text = (cert_nodes[0].text or "").strip()
        if self._has_whitespace(cert_text):
            raise SIIError(
                "X509Certificate contiene espacios o saltos al verificar."
            )

        signature_node = xmlsec.tree.find_node(
            root,
            xmlsec.constants.NodeSignature,
            xmlsec.constants.DSigNs,
        )
        if signature_node is None:
            raise SIIError("No se encontró Signature para verificar.")

        ctx = xmlsec.SignatureContext()
        ctx.key = self._make_xmlsec_key()
        ctx.verify(signature_node)
        return True

    def certificate_rut(self):
        """
        Retorna el RUT del titular del certificado cuando viene en
        serialNumber (OID 2.5.4.5), por ejemplo 10346722-5.
        """
        try:
            attrs = self.certificate.subject.get_attributes_for_oid(
                NameOID.SERIAL_NUMBER
            )
            if attrs:
                return str(attrs[0].value).strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _split_rut(rut):
        clean = re.sub(r"[^0-9Kk]", "", rut or "").upper()
        if len(clean) < 2:
            raise SIIError(f"RUT inválido: {rut}")
        return clean[:-1], clean[-1]

    def query_dte_status(
        self,
        token,
        consultant_rut,
        company_rut,
        receiver_rut,
        dte_type,
        folio,
        issue_date_ddmmyyyy,
        total_amount,
    ):
        """
        Consulta oficial QueryEstDte.jws para un DTE conocido.
        Retorna ESTADO, GLOSA, ERR_CODE, GLOSA_ERR y NUM_ATENCION.
        """
        rut_cons, dv_cons = self._split_rut(consultant_rut)
        rut_comp, dv_comp = self._split_rut(company_rut)
        rut_rec, dv_rec = self._split_rut(receiver_rut)

        ns = f"https://{self.host}/DTEWS/QueryEstDte.jws"
        url = ns

        params = [
            ("RutConsultante", rut_cons),
            ("DvConsultante", dv_cons),
            ("RutCompania", rut_comp),
            ("DvCompania", dv_comp),
            ("RutReceptor", rut_rec),
            ("DvReceptor", dv_rec),
            ("TipoDte", str(dte_type)),
            ("FolioDte", str(folio)),
            ("FechaEmisionDte", str(issue_date_ddmmyyyy)),
            ("MontoDte", str(int(total_amount))),
            ("Token", str(token)),
        ]

        values = "".join(
            f'<{name} xsi:type="xsd:string">{html.escape(value)}</{name}>'
            for name, value in params
        )

        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<SOAP-ENV:Envelope xmlns:SOAP-ENV="{self.SOAP_NS}" '
            f'xmlns:SOAP-ENC="{self.SOAP_ENC}" '
            f'xmlns:xsi="{self.XSI_NS}" '
            f'xmlns:xsd="{self.XSD_NS}" '
            f'SOAP-ENV:encodingStyle="{self.SOAP_ENC}">'
            '<SOAP-ENV:Body>'
            f'<m:getEstDte xmlns:m="{ns}">'
            f'{values}'
            '</m:getEstDte>'
            '</SOAP-ENV:Body>'
            '</SOAP-ENV:Envelope>'
        ).encode("utf-8")

        response = self._post_soap(
            url,
            body,
            "last_query_dte_request.xml",
        )
        Path("last_query_dte_soap_response.xml").write_bytes(response)

        inner = self._soap_string(response, "getEstDteReturn")
        Path("last_query_dte_response.xml").write_text(
            inner, encoding="utf-8"
        )

        root = etree.fromstring(inner.encode("utf-8"))

        def x(name):
            return root.xpath(f"string(//*[local-name()='{name}'])")

        return {
            "estado": x("ESTADO"),
            "glosa": x("GLOSA"),
            "err_code": x("ERR_CODE"),
            "glosa_err": x("GLOSA_ERR"),
            "num_atencion": x("NUM_ATENCION"),
        }

    def get_token(self, signed_xml):
        # Bloqueo final antes de enviar.
        parsed = etree.fromstring(signed_xml)
        cert_nodes = parsed.xpath(
            "//*[local-name()='X509Certificate' and namespace-uri()=$ns]",
            ns=self.DSIG_NS,
        )
        if len(cert_nodes) != 1:
            raise SIIError(
                "Bloqueo de seguridad: X509Certificate inexistente o duplicado."
            )

        cert_text = (cert_nodes[0].text or "").strip()
        if not cert_text:
            raise SIIError(
                "Bloqueo de seguridad: X509Certificate vacío."
            )
        if self._has_whitespace(cert_text):
            raise SIIError(
                "Bloqueo de seguridad: X509Certificate contiene espacios o saltos. "
                "No se enviará al SII."
            )

        ns = f"https://{self.host}/DTEWS/GetTokenFromSeed.jws"

        signed_text = signed_xml.decode("ISO-8859-1")
        escaped = html.escape(signed_text, quote=True)

        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<SOAP-ENV:Envelope xmlns:SOAP-ENV="{self.SOAP_NS}" '
            f'xmlns:SOAP-ENC="{self.SOAP_ENC}" '
            f'xmlns:xsi="{self.XSI_NS}" '
            f'xmlns:xsd="{self.XSD_NS}" '
            f'SOAP-ENV:encodingStyle="{self.SOAP_ENC}">'
            '<SOAP-ENV:Body>'
            f'<m:getToken xmlns:m="{ns}">'
            f'<pszXml xsi:type="xsd:string">{escaped}</pszXml>'
            '</m:getToken>'
            '</SOAP-ENV:Body>'
            '</SOAP-ENV:Envelope>'
        ).encode("utf-8")

        response = self._post_soap(
            self.token_url,
            body,
            "last_token_request_v4_5.xml",
        )
        Path("last_token_soap_response_v4_5.xml").write_bytes(response)

        inner = self._soap_string(response, "getTokenReturn")
        Path("last_token_response_v4_5.xml").write_text(
            inner, encoding="utf-8"
        )

        root = etree.fromstring(inner.encode("utf-8"))
        estado = root.xpath("string(//*[local-name()='ESTADO'])")
        token = root.xpath("string(//*[local-name()='TOKEN'])")
        glosa = root.xpath("string(//*[local-name()='GLOSA'])")

        if estado != "00" or not token:
            raise SIIError(
                f"SII rechazó token. Estado {estado}: {glosa}. "
                "Respuesta guardada en last_token_response_v4_5.xml"
            )

        return token.strip()
