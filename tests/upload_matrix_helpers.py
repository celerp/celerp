"""Valid small and large PDFs and images for the shared attachment matrix."""
import io
from PIL import Image
from reportlab.pdfgen.canvas import Canvas

def pdf(large=False):
    out=io.BytesIO(); canvas=Canvas(out, pageCompression=0)
    canvas.setSubject('Scan metadata ' * 170000 if large else 'Attachment regression')
    canvas.drawString(72,720,'Celerp attachment regression');canvas.save()
    return out.getvalue()

def image(kind):
    out=io.BytesIO();Image.new('RGBA' if kind=='PNG' else 'RGB',(32,32)).save(out,format=kind)
    return out.getvalue()

FILES=[('small.PDF','application/pdf',pdf()),('large scan.pdf','application/pdf',pdf(True)),
       ('alpha.png','image/png',image('PNG')),('photo.jpg','image/jpeg',image('JPEG'))]
DOC_TYPES=['invoice','proforma','purchase_order','quotation','shipping_doc','credit_note','debit_note','bill','memo','list','transfer','consignment_in','subscription_invoice','subscription_po','production_order','audit','writeoff']
OWNERS=[('docs',k,False) for k in DOC_TYPES]+[('lists',k,False) for k in ['quotation','transfer','audit','writeoff','shipping_doc']]+[('docs',k,True) for k in ['invoice','memo','purchase_order','bill','consignment_in']]+[('lists','quotation',True)]
