from flask import Flask, render_template, request, redirect, url_for, send_file, flash
import pandas as pd
import xmltodict
import PyPDF2
import os
import re
import unicodedata
from werkzeug.utils import secure_filename
from datetime import datetime

app = Flask(__name__)

# Configuración
app.config.update({
    'UPLOAD_FOLDER': os.path.join(os.getcwd(), 'facturas_generadas'),
    'ALLOWED_EXTENSIONS': {'xml', 'pdf'},
    'SECRET_KEY': 'tu_clave_secreta_aqui',
    'MAX_CONTENT_LENGTH': 16 * 1024 * 1024  # 16MB
})

# --------------------------
# FUNCIONES DE UTILIDAD
# --------------------------

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def sanitize_filename(filename):
    """Normaliza nombres de archivo"""
    filename = unicodedata.normalize('NFKD', filename).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^\w\-_. ]', '', filename)

def get_safe_upload_folder():
    """Obtiene una carpeta segura para uploads"""
    try:
        base_dir = app.config['UPLOAD_FOLDER']
        os.makedirs(base_dir, exist_ok=True)
        # Verificar permisos
        test_file = os.path.join(base_dir, 'test_permission.tmp')
        with open(test_file, 'w') as f:
            f.write('test')
        os.remove(test_file)
        return base_dir
    except Exception as e:
        print(f"Error con directorio principal: {e}")
        # Usar temporal como respaldo
        import tempfile
        temp_dir = os.path.join(tempfile.gettempdir(), 'facturas_temp')
        os.makedirs(temp_dir, exist_ok=True)
        return temp_dir

# --------------------------
# PROCESAMIENTO DE FACTURAS (VERSIÓN MEJORADA)
# --------------------------

def parse_invoice_text(text):
    """Analiza el texto de factura PDF con extracción robusta de campos"""
    try:
        # 1. FECHA - Patrones mejorados
        fecha = ''
        fecha_patterns = [
            r'(?:Fecha|Fecha\s+de\s+emisi[óo]n|Fecha\s+y\s+hora)\s*[:]?\s*(\d{2}/\d{2}/\d{4})',
            r'(?:Fecha|Fecha\s+de\s+emisi[óo]n)\s*[:]?\s*(\d{2}-\d{2}-\d{4})',
            r'\b(\d{2}/\d{2}/\d{4})\b',
            r'\b(\d{2}-\d{2}-\d{4})\b',
            r'(?:Fecha|Fecha\s+de\s+emisi[óo]n)\s*[:]?\s*(\d{1,2}\s*[/-]\s*[A-Za-z]+\s*[/-]\s*\d{4})'
        ]
        
        for pattern in fecha_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                fecha = match.group(1).replace('-', '/')
                break

        # 2. NÚMERO DE FACTURA - EXTRACCIÓN MEJORADA
        factura_num = ''
        factura_patterns = [
            r'(?:Factura|Folio|N[úu]mero\s+de\s+factura)\s*[:]?\s*([A-Z]{2,}\s*[-]?\s*\d+)',  # FAS-1001 o FAS 1001
            r'(?:No\.?|Número)\s*[:]?\s*(\d+)',  # No. 1234
            r'Factura\s+FASALA\s*[-]?\s*(\d+)',  # Factura FASALA-1001
            r'FASALA\s*[-]?\s*(\d+)',  # FASALA-1001
            r'FACTURA\s*[:]?\s*([A-Z0-9-]+)'  # FACTURA: ABC123
        ]
        
        for pattern in factura_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                factura_num = match.group(1).replace(' ', '-')  # Normalizar espacios a guiones
                break

        # 3. PROVEEDOR
        proveedor = ''
        proveedor_match = re.search(
            r'(?:PROVEEDOR|PRODUCTORA|EMISOR|RAZ[ÓO]N\s+SOCIAL)\s*[:]?\s*(.+?)\n', 
            text, 
            re.IGNORECASE
        )
        if proveedor_match:
            proveedor = proveedor_match.group(1).strip()

        # 4. PRODUCTOS
        producto_pattern = r'(\d+\.\d+)\s+(\d+\.\d+)\s+([\d,]+\.\d{2})\s+(.+?)\s+([A-Z]{2,3})\b'
        productos = []
        
        for partida_num, match in enumerate(re.finditer(producto_pattern, text), 1):
            try:
                productos.append({
                    'fecha': fecha,
                    'proveedor': proveedor,
                    'factura': factura_num,
                    'partida': partida_num,
                    'cantidad': float(match.group(1)),
                    'unidad': match.group(5),
                    'descripcion': ' '.join(match.group(4).strip().split()),
                    'precio_unitario': float(match.group(2)),
                    'sub_total': float(match.group(3).replace(',', '')),
                    'iva': 0.0,
                    'total': float(match.group(3).replace(',', ''))
                })
            except Exception as e:
                print(f"Error procesando producto {partida_num}: {e}")
                continue
                
        return productos
        
    except Exception as e:
        print(f"Error grave al parsear texto: {e}")
        return []

def parse_xml_file(xml_path):
    """Procesa archivos XML CFDI"""
    try:
        with open(xml_path, 'r', encoding='utf-8') as f:
            data_dict = xmltodict.parse(f.read())

        cfdi = data_dict.get('cfdi:Comprobante', {})
        fecha = cfdi.get('@Fecha', '')[:10]  # Solo fecha sin hora
        proveedor = cfdi.get('cfdi:Emisor', {}).get('@Nombre', '')
        factura_num = cfdi.get('@Folio', '') or (cfdi.get('@Serie', '') + '-' + cfdi.get('@Folio', '')) if cfdi.get('@Folio') else ''

        conceptos = cfdi.get('cfdi:Conceptos', {}).get('cfdi:Concepto', [])
        if not isinstance(conceptos, list):
            conceptos = [conceptos]

        productos = []
        for partida_num, concepto in enumerate(conceptos, 1):
            try:
                productos.append({
                    'fecha': fecha,
                    'proveedor': proveedor,
                    'factura': factura_num,
                    'partida': partida_num,
                    'cantidad': float(concepto.get('@Cantidad', 0)),
                    'unidad': concepto.get('@Unidad', ''),
                    'descripcion': concepto.get('@Descripcion', ''),
                    'precio_unitario': float(concepto.get('@ValorUnitario', 0)),
                    'sub_total': float(concepto.get('@Importe', 0)),
                    'iva': 0.0,
                    'total': float(concepto.get('@Importe', 0))
                })
            except Exception as e:
                print(f"Error procesando concepto {partida_num}: {e}")
                continue

        return productos

    except Exception as e:
        print(f"Error al procesar XML: {e}")
        return []

# --------------------------
# GENERACIÓN DE EXCEL
# --------------------------

def create_excel(data, output_path):
    """Genera archivo Excel con validación mejorada"""
    try:
        if not data:
            print("Error: No hay datos para generar Excel")
            return False
            
        df = pd.DataFrame(data)
        
        # Verificar campos críticos
        if df.empty or 'descripcion' not in df.columns or 'sub_total' not in df.columns:
            print("Error: Datos incompletos para generar Excel")
            return False
            
        # Ordenar columnas
        column_order = [
            'fecha', 'proveedor', 'factura', 'partida',
            'cantidad', 'unidad', 'descripcion',
            'precio_unitario', 'sub_total', 'iva', 'total'
        ]
        
        # Seleccionar solo columnas existentes
        final_columns = [col for col in column_order if col in df.columns]
        df = df[final_columns]
        
        # Generar Excel
        df.columns = [col.upper() for col in df.columns]
        with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='FACTURA', index=False)
            
        return True
        
    except Exception as e:
        print(f"Error al generar Excel: {str(e)}")
        return False

# --------------------------
# RUTA PRINCIPAL
# --------------------------

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        # Verificación inicial
        file = request.files.get('pdf_file') or request.files.get('xml_file')
        if not file or file.filename == '':
            flash('No se seleccionó ningún archivo', 'error')
            return redirect(request.url)

        if not allowed_file(file.filename):
            flash('Formato no válido. Solo se permiten archivos PDF o XML.', 'error')
            return redirect(request.url)

        try:
            # Configuración de rutas
            upload_folder = get_safe_upload_folder()
            
            # Guardar archivo temporal
            temp_ext = file.filename.rsplit('.', 1)[1].lower()
            temp_filename = secure_filename(f"temp_{datetime.now().timestamp()}.{temp_ext}")
            temp_path = os.path.join(upload_folder, temp_filename)
            file.save(temp_path)

            # Procesamiento según tipo
            if temp_ext == 'pdf':
                try:
                    # Extraer texto con manejo de errores
                    pdf_reader = PyPDF2.PdfReader(temp_path)
                    pdf_text = "\n".join(page.extract_text() or '' for page in pdf_reader.pages)
                    
                    # Debug: Guardar texto extraído
                    debug_path = os.path.join(upload_folder, 'debug_pdf_text.txt')
                    with open(debug_path, 'w', encoding='utf-8') as f:
                        f.write(pdf_text)
                    
                    data = parse_invoice_text(pdf_text)
                    
                    if not data:
                        flash('No se pudieron extraer datos del PDF. Verifique el formato.', 'error')
                        return redirect(request.url)
                        
                except Exception as e:
                    flash(f'Error al procesar PDF: {str(e)}', 'error')
                    return redirect(request.url)
            else:
                data = parse_xml_file(temp_path)

            # Generar Excel
            output_filename = sanitize_filename(f"factura_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
            output_path = os.path.join(upload_folder, output_filename)
            
            if not create_excel(data, output_path):
                flash('Error al generar el archivo Excel. Los datos pueden estar incompletos.', 'error')
                return redirect(request.url)

            # Limpieza y envío
            os.remove(temp_path)
            return send_file(
                output_path,
                as_attachment=True,
                download_name=output_filename,
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )

        except Exception as e:
            # Limpieza en caso de error
            if 'temp_path' in locals() and os.path.exists(temp_path):
                os.remove(temp_path)
            if 'output_path' in locals() and os.path.exists(output_path):
                os.remove(output_path)
                
            flash(f'Error en el proceso: {str(e)}', 'error')
            return redirect(request.url)

    return render_template('index.html')

# --------------------------
# INICIALIZACIÓN
# --------------------------

if __name__ == '__main__':
    # Crear directorio al iniciar
    try:
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    except Exception as e:
        print(f"Error al crear directorio: {e}")
    
    app.run(debug=True)
