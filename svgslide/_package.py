"""Write a minimal PresentationML package with zipfile."""

import io
import zipfile

NS = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
      'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"')
HDR = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
CT = 'application/vnd.openxmlformats-officedocument.'
EMPTY_TREE = ('<p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/>'
              '</p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
              '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>%s</p:spTree>')
MIME = {'png': 'image/png', 'jpeg': 'image/jpeg', 'gif': 'image/gif'}


def _rels(items):
    body = ''.join('<Relationship Id="%s" Type="%s" Target="%s"/>' % (i, R + t if '://' not in t else t, tg)
                   for i, t, tg in items)
    return (HDR + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + body + '</Relationships>')


def _theme():
    def clr(name, val):
        return '<a:%s><a:srgbClr val="%s"/></a:%s>' % (name, val, name)
    colors = (clr('dk1', '000000') + clr('lt1', 'FFFFFF') + clr('dk2', '44546A') + clr('lt2', 'E7E6E6') +
              clr('accent1', '4472C4') + clr('accent2', 'ED7D31') + clr('accent3', 'A5A5A5') +
              clr('accent4', 'FFC000') + clr('accent5', '5B9BD5') + clr('accent6', '70AD47') +
              clr('hlink', '0563C1') + clr('folHlink', '954F72'))
    font = '<a:latin typeface="Arial"/><a:ea typeface=""/><a:cs typeface=""/>'
    fill = '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    ln = ('<a:ln w="%d" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/>'
          '</a:solidFill><a:prstDash val="solid"/><a:miter lim="800000"/></a:ln>')
    eff = '<a:effectStyle><a:effectLst/></a:effectStyle>'
    return (HDR + '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="svgslide">'
            '<a:themeElements><a:clrScheme name="svgslide">' + colors + '</a:clrScheme>'
            '<a:fontScheme name="svgslide"><a:majorFont>' + font + '</a:majorFont><a:minorFont>' + font +
            '</a:minorFont></a:fontScheme><a:fmtScheme name="svgslide"><a:fillStyleLst>' + fill * 3 +
            '</a:fillStyleLst><a:lnStyleLst>' + ln % 6350 + ln % 12700 + ln % 19050 +
            '</a:lnStyleLst><a:effectStyleLst>' + eff * 3 + '</a:effectStyleLst><a:bgFillStyleLst>' +
            fill * 3 + '</a:bgFillStyleLst></a:fmtScheme></a:themeElements>'
            '<a:objectDefaults/><a:extraClrSchemeLst/></a:theme>')


def build(slides, slide_w, slide_h):
    """Return the .pptx bytes.

    slides = [(shapes_xml, media)], media = [(filename, bytes)] whose
    relationship ids in shapes_xml are rId2.. in order.
    """
    names = []          # per slide: its media's package file names
    n = 0
    for _, media in slides:
        own = []
        for name, _ in media:
            n += 1
            own.append('image%d.%s' % (n, name.rsplit('.', 1)[1]))
        names.append(own)
    exts = sorted(set(nm.rsplit('.', 1)[1] for own in names for nm in own))
    ns = len(slides)
    ct = (HDR + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>' +
          ''.join('<Default Extension="%s" ContentType="%s"/>' % (e, MIME[e]) for e in exts) +
          '<Override PartName="/ppt/presentation.xml" ContentType="%spresentationml.presentation.main+xml"/>'
          '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="%spresentationml.slideMaster+xml"/>'
          '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="%spresentationml.slideLayout+xml"/>'
          % ((CT,) * 3) +
          ''.join('<Override PartName="/ppt/slides/slide%d.xml" ContentType="%spresentationml.slide+xml"/>'
                  % (k + 1, CT) for k in range(ns)) +
          '<Override PartName="/ppt/theme/theme1.xml" ContentType="%stheme+xml"/>'
          '<Override PartName="/ppt/presProps.xml" ContentType="%spresentationml.presProps+xml"/>'
          '<Override PartName="/ppt/viewProps.xml" ContentType="%spresentationml.viewProps+xml"/>'
          '<Override PartName="/ppt/tableStyles.xml" ContentType="%spresentationml.tableStyles+xml"/>'
          '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
          '<Override PartName="/docProps/app.xml" ContentType="%sextended-properties+xml"/>'
          '</Types>' % ((CT,) * 5))
    pres = (HDR + '<p:presentation %s saveSubsetFonts="1"><p:sldMasterIdLst>'
            '<p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
            '<p:sldIdLst>%s</p:sldIdLst>'
            '<p:sldSz cx="%d" cy="%d"/><p:notesSz cx="6858000" cy="9144000"/>'
            '<p:defaultTextStyle><a:defPPr><a:defRPr lang="en-US"/></a:defPPr></p:defaultTextStyle>'
            '</p:presentation>') % (NS, ''.join('<p:sldId id="%d" r:id="rId%d"/>' % (256 + k, 6 + k if k else 2)
                                                for k in range(ns)), slide_w, slide_h)
    master = (HDR + '<p:sldMaster %s><p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef>'
              '</p:bg>%s</p:cSld><p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" '
              'accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" '
              'hlink="hlink" folHlink="folHlink"/><p:sldLayoutIdLst>'
              '<p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst><p:txStyles>'
              '<p:titleStyle><a:lvl1pPr><a:defRPr/></a:lvl1pPr></p:titleStyle>'
              '<p:bodyStyle><a:lvl1pPr><a:defRPr/></a:lvl1pPr></p:bodyStyle>'
              '<p:otherStyle><a:lvl1pPr><a:defRPr/></a:lvl1pPr></p:otherStyle></p:txStyles>'
              '</p:sldMaster>') % (NS, EMPTY_TREE % '')
    layout = (HDR + '<p:sldLayout %s type="blank" preserve="1"><p:cSld name="Blank">%s</p:cSld>'
              '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>') % (NS, EMPTY_TREE % '')
    # the first slide is rId2, further slides rId7 on
    pres_rels = [('rId1', 'slideMaster', 'slideMasters/slideMaster1.xml'),
                 ('rId2', 'slide', 'slides/slide1.xml'),
                 ('rId3', 'presProps', 'presProps.xml'),
                 ('rId4', 'viewProps', 'viewProps.xml'),
                 ('rId5', 'theme', 'theme/theme1.xml'),
                 ('rId6', 'tableStyles', 'tableStyles.xml')]
    pres_rels += [('rId%d' % (6 + k), 'slide', 'slides/slide%d.xml' % (k + 1)) for k in range(1, ns)]
    files = [
        ('[Content_Types].xml', ct),
        ('_rels/.rels', _rels([
            ('rId1', 'officeDocument', 'ppt/presentation.xml'),
            ('rId2', 'http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties',
             'docProps/core.xml'),
            ('rId3', 'extended-properties', 'docProps/app.xml')])),
        ('docProps/core.xml', HDR + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
         'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>svgslide</dc:creator>'
         '</cp:coreProperties>'),
        ('docProps/app.xml', HDR + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
         'extended-properties"><Application>svgslide</Application><Slides>%d</Slides></Properties>' % ns),
        ('ppt/presentation.xml', pres),
        ('ppt/_rels/presentation.xml.rels', _rels(pres_rels)),
        ('ppt/presProps.xml', HDR + '<p:presentationPr %s/>' % NS),
        ('ppt/viewProps.xml', HDR + '<p:viewPr %s><p:gridSpacing cx="76200" cy="76200"/></p:viewPr>' % NS),
        ('ppt/tableStyles.xml', HDR + '<a:tblStyleLst xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/'
         'main" def="{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}"/>'),
        ('ppt/theme/theme1.xml', _theme()),
        ('ppt/slideMasters/slideMaster1.xml', master),
        ('ppt/slideMasters/_rels/slideMaster1.xml.rels', _rels([
            ('rId1', 'slideLayout', '../slideLayouts/slideLayout1.xml'),
            ('rId2', 'theme', '../theme/theme1.xml')])),
        ('ppt/slideLayouts/slideLayout1.xml', layout),
        ('ppt/slideLayouts/_rels/slideLayout1.xml.rels', _rels([
            ('rId1', 'slideMaster', '../slideMasters/slideMaster1.xml')])),
    ]
    for k, (shapes_xml, media) in enumerate(slides):
        slide = (HDR + '<p:sld %s><p:cSld>%s</p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>'
                 '</p:sld>') % (NS, EMPTY_TREE % shapes_xml)
        slide_rels = [('rId1', 'slideLayout', '../slideLayouts/slideLayout1.xml')]
        slide_rels += [('rId%d' % (i + 2), 'image', '../media/' + nm) for i, nm in enumerate(names[k])]
        files.append(('ppt/slides/slide%d.xml' % (k + 1), slide))
        files.append(('ppt/slides/_rels/slide%d.xml.rels' % (k + 1), _rels(slide_rels)))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, text in files:
            _write(z, name, text.encode('utf-8'), zipfile.ZIP_DEFLATED)
        for own, (_, media) in zip(names, slides):
            for nm, (_, data) in zip(own, media):
                _write(z, 'ppt/media/' + nm, data, zipfile.ZIP_STORED)
    return buf.getvalue()


def _write(z, name, data, method):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = method
    info.create_system = 0
    info.external_attr = 0
    z.writestr(info, data)
