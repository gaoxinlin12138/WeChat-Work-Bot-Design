const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(
  path.join(__dirname, '../../app/admin/static/product-admin.js'), 'utf8'
);

function publishedProduct() {
  const snapshot = {
    model: 'TEST-001', set_price: 170, cost_price: 90, body_price: 110,
    components: [{name: '说明书', supplier: '测试供应商', unit_price: 1.5}],
    media_ids: ['main', 'gallery'],
  };
  return {
    ...structuredClone(snapshot), id: 'test-product', status: 'active',
    set_price: '170.00', cost_price: '90.00', body_price: '110.00',
    media: [{id: 'gallery'}, {id: 'main'}], published_snapshot: snapshot,
  };
}

async function renderProduct(product, interact) {
  // Run the shipped catalog script with in-memory API and DOM adapters.
  // No real product is edited, published, or sent to the robot.
  const elements = new Map();
  function element(selector) {
    if (!elements.has(selector)) elements.set(selector, {
      value: '', files: [], innerHTML: '', textContent: '',
      reset() {}, reportValidity: () => true,
      addEventListener() {}, querySelectorAll: () => [],
      elements: new Proxy({}, {get: (_target, name) => element(`field:${String(name)}`)}),
      showModal() {this.open = true;}, close() {this.open = false;},
      focus() {},
    });
    return elements.get(selector);
  }
  const sandbox = {
    window: {}, state: {catalog: [], selectedCatalogIds: new Set()},
    $: element, $$: () => [], document: {querySelector: element},
    api: async url => {
      assert.equal(url, '/business/products?include_deleted=true');
      return [product];
    },
    esc: value => String(value ?? ''), money: value => Number(value).toFixed(2),
    badge: status => `<span>${status === 'active' ? '已发布' : '草稿'}</span>`,
    toast: message => {throw new Error(message);},
    setCatalogModule() {}, requestAnimationFrame: callback => callback(),
  };
  sandbox.window = sandbox;
  vm.runInNewContext(source, sandbox, {filename: 'product-admin.js'});
  await sandbox.loadCatalog();
  if(interact)await interact(sandbox,element);
  return element('#catalog-list').innerHTML;
}

function assertPending(html, pending) {
  for (const text of ['有修改待发布', '发布修改', '机器人仍使用发布前内容']) {
    assert.equal(html.includes(text), pending, text);
  }
  assert.ok(html.includes('已发布'));
}

test('published decimal strings and numeric snapshots show only published status', async () => {
  assertPending(await renderProduct(publishedProduct()), false);
});

test('actual edits to each price remain pending, including null versus zero', async () => {
  for (const field of ['set_price', 'cost_price', 'body_price']) {
    const product = publishedProduct();
    product[field] = '170.01';
    assertPending(await renderProduct(product), true);
    product[field] = null;
    product.published_snapshot[field] = 0;
    assertPending(await renderProduct(product), true);
    product[field] = '0.00';
    assertPending(await renderProduct(product), false);
  }
});

test('component key order and price formatting do not create changes', async () => {
  const product = publishedProduct();
  product.components = [{unit_price: '1.50', supplier: '测试供应商', name: '说明书'}];
  assertPending(await renderProduct(product), false);
  product.components[0].unit_price = '1.51';
  assertPending(await renderProduct(product), true);
  product.components[0].unit_price = '1.50';
  product.components[0].supplier = '另一供应商';
  assertPending(await renderProduct(product), true);
});

test('text and identifier changes are not normalized as numbers', async () => {
  const product = publishedProduct();
  product.model = '001';
  product.published_snapshot.model = '1';
  assertPending(await renderProduct(product), true);
});

test('media additions and removals remain pending', async () => {
  const product = publishedProduct();
  product.media.push({id: 'new-image'});
  assertPending(await renderProduct(product), true);
  product.media = [{id: 'main'}];
  assertPending(await renderProduct(product), true);
});

test('a refreshed published snapshot clears an actual pending edit', async () => {
  const product = publishedProduct();
  product.set_price = '171.00';
  assertPending(await renderProduct(product), true);
  product.published_snapshot.set_price = 171;
  assertPending(await renderProduct(product), false);
});

test('stock column follows price and distinguishes unknown from zero', async () => {
  for (const count of [null, 0, 120, 2147483647]) {
    const product = publishedProduct();
    product.piece_count = count;
    product.published_snapshot.piece_count = count;
    const html = await renderProduct(product);
    assertPending(html, false);
    assert.ok(html.indexOf('catalog-commercial') < html.indexOf('catalog-piece-count'));
    assert.ok(html.indexOf('catalog-piece-count') < html.indexOf('catalog-counts'));
    assert.ok(html.includes(count === null ? '未登记' : `${count}<small> 件</small>`));
  }
});

test('stock draft edits stay pending until published', async () => {
  const product = publishedProduct();
  product.piece_count = 0;
  product.published_snapshot.piece_count = 120;
  assertPending(await renderProduct(product), true);
  product.published_snapshot.piece_count = 0;
  assertPending(await renderProduct(product), false);
  product.piece_count = null;
  assertPending(await renderProduct(product), true);
});

test('editor fills and submits stock counts, including explicit empty values', async () => {
  for(const count of [120, 0, null]) {
    const product = publishedProduct();
    product.piece_count = count;
    product.components = [];
    product.media = [];
    await renderProduct(product, async (sandbox, element) => {
      sandbox.editCatalog(product.id);
      assert.equal(element('field:piece_count').value, count ?? '');
      // Browsers expose number-input values as strings.
      for(const [name,value] of Object.entries(product)) {
        element(`field:${name}`).value = value == null ? '' : String(value);
      }
      element('field:piece_count').value = count == null ? '' : String(count);
      for(const name of ['sequence_number','series','product_description','material','spray_modes',
        'color','special_function','shipping_origin','supplier','body_supplier','notes']) {
        element(`field:${name}`).value = String(product[name] ?? '');
      }
      let submitted;
      sandbox.api = async (_url, options) => {
        submitted = JSON.parse(options.body);
        return new Promise(() => {});
      };
      element('#product-form').onsubmit({preventDefault() {}});
      assert.equal(submitted.piece_count, count);
    });
  }
});
