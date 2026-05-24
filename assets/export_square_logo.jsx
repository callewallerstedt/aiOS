#target photoshop
app.displayDialogs = DialogModes.NO;

(function () {
    var outFolder = new Folder('C:/CODEX/Computer helper/assets');
    if (!outFolder.exists) outFolder.create();

    if (app.documents.length === 0) {
        throw new Error('No active Photoshop document found.');
    }

    var src = app.activeDocument;

    function hexColor(hex) {
        var c = new SolidColor();
        c.rgb.hexValue = hex.replace('#', '');
        return c;
    }

    function addBackground(doc, hex) {
        var bg = doc.artLayers.add();
        bg.name = 'background-' + hex;
        doc.activeLayer = bg;
        doc.selection.selectAll();
        doc.selection.fill(hexColor(hex), ColorBlendMode.NORMAL, 100, false);
        doc.selection.deselect();
        bg.move(doc, ElementPlacement.PLACEATEND);
    }

    function exportPng(doc, file, transparency) {
        var opts = new ExportOptionsSaveForWeb();
        opts.format = SaveDocumentType.PNG;
        opts.PNG8 = false;
        opts.transparency = transparency;
        opts.interlaced = false;
        opts.quality = 100;
        doc.exportDocument(file, ExportType.SAVEFORWEB, opts);
    }

    function makeVersion(name, sizePx, padPct, bgHex) {
        var doc = src.duplicate('export-' + name, false);
        app.activeDocument = doc;

        // Remove empty transparent border around the actual logo artwork.
        doc.trim(TrimType.TRANSPARENT, true, true, true, true);

        var w = doc.width.as('px');
        var h = doc.height.as('px');
        var target = Math.round(sizePx * (1 - (padPct * 2)));

        // Scale proportionally so the longest side fits inside the padded square.
        if (w >= h) {
            doc.resizeImage(UnitValue(target, 'px'), null, 300, ResampleMethod.BICUBICSHARPER);
        } else {
            doc.resizeImage(null, UnitValue(target, 'px'), 300, ResampleMethod.BICUBICSHARPER);
        }

        // Center on exact square canvas with transparent padding.
        doc.resizeCanvas(UnitValue(sizePx, 'px'), UnitValue(sizePx, 'px'), AnchorPosition.MIDDLECENTER);

        if (bgHex !== null) {
            addBackground(doc, bgHex);
        }

        var outFile = new File(outFolder.fsName + '/' + name + '.png');
        exportPng(doc, outFile, bgHex === null);
        doc.close(SaveOptions.DONOTSAVECHANGES);
    }

    // Main transparent square logo exports with clean padding.
    makeVersion('aisos-logo-square-1024', 1024, 0.10, null);
    makeVersion('aisos-logo-square-512', 512, 0.10, null);
    makeVersion('aisos-logo-square-256', 256, 0.10, null);

    // Slightly tighter option for places that need stronger canvas fill.
    makeVersion('aisos-logo-square-tight-1024', 1024, 0.06, null);
    makeVersion('aisos-logo-square-tight-512', 512, 0.06, null);

    // Dark preview versions so the white mark is visible on a square background.
    makeVersion('aisos-logo-square-dark-1024', 1024, 0.10, '111827');
    makeVersion('aisos-logo-square-dark-512', 512, 0.10, '111827');

    app.activeDocument = src;
})();
