/**
 * Rendering: syntax highlighting (Primer .pl-* classes) and minimal Markdown.
 * No external dependency - the page must work offline, over file://.
 */
window.Render = (function () {
  const escapeHtml = (text) =>
    text.replace(/[&<>"']/g, (char) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[char])

  const span = (className, text) =>
    className && text ? `<span class="${className}">${escapeHtml(text)}</span>` : escapeHtml(text)

  const words = (list) => new Set(list.split(' '))

  const CLIKE = 'break case catch class const continue default do else enum extends false finally' +
    ' for function goto if implements import in instanceof interface new null package private' +
    ' protected public return static super switch this throw true try typeof var void while with'

  /**
   * One tokenizer, configured per language. Highlighting runs line by line, but the state
   * (an open string or comment) has to survive from one line to the next: a repository's SQL
   * queries, a JS template literal or a Python docstring span several lines.
   */
  function clike(spec) {
    const cfg = Object.assign({
      line: [], block: [], strings: [], extra: [],
      keywords: new Set(), literals: new Set(), types: new Set(),
      member: ['->', '::', '.'], fold: false,
      ident: /^[A-Za-z_][A-Za-z0-9_]*/,
      number: /^(0[xXbBoO][0-9a-fA-F_]+|\d[\d_]*(\.[0-9_]+)?([eE][+-]?\d+)?)/,
    }, spec)

    return function () {
      let open = null

      return function highlightLine(source) {
        let out = ''
        let index = 0
        let lastWord = ''

        while (index < source.length) {
          if (open) {
            let cursor = index
            let found = -1
            while (cursor <= source.length - open.close.length) {
              if (open.esc && source[cursor] === '\\') { cursor += 2; continue }
              if (source.startsWith(open.close, cursor)) { found = cursor; break }
              cursor++
            }
            if (found < 0) { return out + span(open.cls, source.slice(index)) }
            out += span(open.cls, source.slice(index, found + open.close.length))
            index = found + open.close.length
            open = null
            continue
          }

          const rest = source.slice(index)
          let matched = false

          for (const rule of cfg.extra) {
            const hit = rest.match(rule.re)
            if (hit) {
              out += span(rule.cls, hit[0])
              index += hit[0].length
              matched = true
              break
            }
          }
          if (matched) continue

          const lineComment = cfg.line.find((token) => rest.startsWith(token))
          if (lineComment) return out + span('pl-c', rest)

          const blockComment = cfg.block.find((pair) => rest.startsWith(pair[0]))
          if (blockComment) {
            open = { close: blockComment[1], cls: 'pl-c', esc: false }
            out += span('pl-c', blockComment[0])
            index += blockComment[0].length
            continue
          }

          const quote = cfg.strings.find((s) => rest.startsWith(s.open))
          if (quote) {
            open = { close: quote.close, cls: quote.cls || 'pl-s', esc: quote.esc !== false }
            out += span(open.cls, quote.open)
            index += quote.open.length
            if (!quote.multiline) {
              const before = index
              let cursor = index
              let found = -1
              while (cursor < source.length) {
                if (open.esc && source[cursor] === '\\') { cursor += 2; continue }
                if (source.startsWith(open.close, cursor)) { found = cursor; break }
                cursor++
              }
              const end = found < 0 ? source.length : found + open.close.length
              out += span(open.cls, source.slice(before, end))
              index = end
              open = null
            }
            continue
          }

          const number = rest.match(cfg.number)
          if (number) {
            out += span('pl-c1', number[0])
            index += number[0].length
            continue
          }

          const word = rest.match(cfg.ident)
          if (word) {
            const token = word[0]
            const after = source.slice(index + token.length).trimStart()
            const isMember = cfg.member.some((op) => lastWord === op)
            const bare = cfg.fold ? token.toLowerCase() : token.replace(/^[\\@]/, '')

            let className = null
            if (!isMember && cfg.keywords.has(bare)) className = 'pl-k'
            else if (!isMember && cfg.literals.has(bare)) className = 'pl-c1'
            else if (!isMember && cfg.types.has(bare)) className = 'pl-c1'
            else if (after.startsWith('(')) className = 'pl-en'
            else if (/^[A-Z][A-Z0-9_]*$/.test(bare) && bare.length > 1) className = 'pl-c1'
            else if (!isMember && /^[A-Z\\]/.test(token)) className = 'pl-c1'

            out += span(className, token)
            index += token.length
            lastWord = token
            continue
          }

          const operator = rest.match(/^(->|::|=>|\?\?|\.)/)
          if (operator) {
            out += escapeHtml(operator[0])
            index += operator[0].length
            lastWord = operator[0]
            continue
          }

          if (source[index] !== ' ' && source[index] !== '\t') lastWord = source[index]
          out += escapeHtml(source[index])
          index += 1
        }

        return out
      }
    }
  }

  const DQ = { open: '"', close: '"' }
  const SQ = { open: "'", close: "'" }

  const php = clike({
    line: ['//'],
    block: [['/*', '*/']],
    strings: [
      { open: '"', close: '"', multiline: true },
      { open: "'", close: "'", multiline: true },
    ],
    extra: [
      { re: /^(<\?php|<\?=|\?>)/, cls: 'pl-k' },
      { re: /^#\[/, cls: 'pl-k' },
      { re: /^#.*/, cls: 'pl-c' },
      { re: /^\$[A-Za-z_][A-Za-z0-9_]*/, cls: 'pl-v' },
    ],
    ident: /^[A-Za-z_\\][A-Za-z0-9_\\]*/,
    keywords: words('abstract and array as break callable case catch class clone const continue' +
      ' declare default do echo else elseif empty enum extends final finally fn for foreach' +
      ' function global goto if implements include include_once instanceof insteadof interface' +
      ' isset list match namespace new or print private protected public readonly require' +
      ' require_once return static switch throw trait try unset use while xor yield'),
    literals: words('true false null TRUE FALSE NULL'),
    types: words('int float bool string void iterable object mixed never self parent array callable'),
  })

  const javascript = clike({
    line: ['//'],
    block: [['/*', '*/']],
    strings: [DQ, SQ, { open: '`', close: '`', multiline: true }],
    ident: /^[A-Za-z_$][A-Za-z0-9_$]*/,
    extra: [{ re: /^@[A-Za-z_][A-Za-z0-9_]*/, cls: 'pl-k' }],
    keywords: words(CLIKE + ' abstract as async await declare delete export from get let of' +
      ' readonly satisfies set type yield namespace module infer keyof asserts is override'),
    literals: words('true false null undefined NaN Infinity'),
    types: words('string number boolean any unknown never object symbol bigint void'),
  })

  const python = clike({
    line: ['#'],
    strings: [
      { open: '"""', close: '"""', multiline: true },
      { open: "'''", close: "'''", multiline: true },
      { open: 'f"', close: '"' }, { open: "f'", close: "'" },
      { open: 'r"', close: '"' }, { open: "r'", close: "'" },
      DQ, SQ,
    ],
    extra: [{ re: /^@[A-Za-z_][A-Za-z0-9_.]*/, cls: 'pl-en' }],
    keywords: words('and as assert async await break class continue def del elif else except' +
      ' finally for from global if import in is lambda nonlocal not or pass raise return try' +
      ' while with yield match case'),
    literals: words('True False None self cls'),
    types: words('int float str bool bytes list dict set tuple frozenset object type'),
  })

  const go = clike({
    line: ['//'],
    block: [['/*', '*/']],
    strings: [DQ, SQ, { open: '`', close: '`', multiline: true }],
    keywords: words('break case chan const continue default defer else fallthrough for func go' +
      ' goto if import interface map package range return select struct switch type var'),
    literals: words('true false nil iota'),
    types: words('bool byte complex64 complex128 error float32 float64 int int8 int16 int32 int64' +
      ' rune string uint uint8 uint16 uint32 uint64 uintptr any'),
  })

  const rust = clike({
    line: ['//'],
    block: [['/*', '*/']],
    strings: [DQ, SQ],
    extra: [{ re: /^#!?\[/, cls: 'pl-k' }, { re: /^'[a-z_]+\b/, cls: 'pl-v' }],
    keywords: words('as async await break const continue crate dyn else enum extern fn for if' +
      ' impl in let loop match mod move mut pub ref return self Self static struct super trait' +
      ' type unsafe use where while'),
    literals: words('true false None Some Ok Err'),
    types: words('bool char f32 f64 i8 i16 i32 i64 i128 isize str u8 u16 u32 u64 u128 usize' +
      ' String Vec Option Result Box'),
  })

  const java = clike({
    line: ['//'],
    block: [['/*', '*/']],
    strings: [DQ, SQ],
    extra: [{ re: /^@[A-Za-z_][A-Za-z0-9_]*/, cls: 'pl-en' }],
    keywords: words(CLIKE + ' abstract assert boolean byte char double final float int long' +
      ' native short strictfp synchronized throws transient volatile fun val when object' +
      ' companion data sealed suspend'),
    literals: words('true false null it'),
    types: words('String Integer Boolean Long Double List Map Set'),
  })

  const cfamily = clike({
    line: ['//'],
    block: [['/*', '*/']],
    strings: [DQ, SQ],
    extra: [{ re: /^#\s*[a-z]+/, cls: 'pl-k' }],
    keywords: words(CLIKE + ' auto bool char double float inline int long namespace operator' +
      ' register short signed sizeof struct template typedef union unsigned using virtual' +
      ' explicit friend mutable constexpr nullptr override final'),
    literals: words('true false NULL nullptr'),
    types: words('size_t int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t'),
  })

  const sql = clike({
    line: ['--'],
    block: [['/*', '*/']],
    strings: [{ open: "'", close: "'", multiline: true }, { open: '`', close: '`' }],
    fold: true,
    keywords: words('select from where insert into values update set delete join inner left right' +
      ' outer full cross on group by having order limit offset union all distinct as and or not' +
      ' null is in exists between like create table alter drop index primary key foreign' +
      ' references constraint default case when then else end with returning'),
    literals: words('true false null'),
  })

  const shell = clike({
    line: ['#'],
    strings: [{ open: '"', close: '"', multiline: true }, { open: "'", close: "'", esc: false }],
    extra: [
      { re: /^\$\{[^}]*\}/, cls: 'pl-v' },
      { re: /^\$[A-Za-z_][A-Za-z0-9_]*/, cls: 'pl-v' },
      { re: /^\$[0-9@*#?]/, cls: 'pl-v' },
    ],
    keywords: words('if then elif else fi for while until do done case esac in function return' +
      ' local export readonly declare shift break continue source exit trap set unset'),
    literals: words('true false'),
  })

  function json() {
    return function highlightLine(source) {
      let out = ''
      const re = /("(?:\\.|[^"\\])*")(\s*:)?|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|\b(true|false|null)\b/g
      let last = 0
      let hit
      while ((hit = re.exec(source))) {
        out += escapeHtml(source.slice(last, hit.index))
        if (hit[1]) out += span(hit[2] ? 'pl-c1' : 'pl-s', hit[1]) + escapeHtml(hit[2] || '')
        else out += span('pl-c1', hit[0])
        last = hit.index + hit[0].length
      }
      return out + escapeHtml(source.slice(last))
    }
  }

  function yaml() {
    return function highlightLine(source) {
      const comment = source.match(/^(\s*)(#.*)$/)
      if (comment) return escapeHtml(comment[1]) + span('pl-c', comment[2])

      const pair = source.match(/^(\s*-?\s*)([A-Za-z0-9_.\-\\]+)(:)(.*)$/)
      if (pair) {
        return escapeHtml(pair[1]) + span('pl-c1', pair[2]) + escapeHtml(pair[3]) +
          (pair[4] ? span('pl-s', pair[4]) : '')
      }

      const item = source.match(/^(\s*-\s+)(.*)$/)
      if (item) return escapeHtml(item[1]) + span('pl-s', item[2])

      return escapeHtml(source)
    }
  }

  function ini() {
    return function highlightLine(source) {
      const comment = source.match(/^(\s*)([#;].*)$/)
      if (comment) return escapeHtml(comment[1]) + span('pl-c', comment[2])

      const section = source.match(/^(\s*)(\[.*\])(.*)$/)
      if (section) return escapeHtml(section[1]) + span('pl-ent', section[2]) + escapeHtml(section[3])

      const pair = source.match(/^(\s*)([A-Za-z0-9_.\-]+)(\s*=\s*)(.*)$/)
      if (pair) {
        return escapeHtml(pair[1]) + span('pl-c1', pair[2]) + escapeHtml(pair[3]) +
          span('pl-s', pair[4])
      }
      return escapeHtml(source)
    }
  }

  function css() {
    let inBlock = false
    let inComment = false

    return function highlightLine(source) {
      let out = ''
      let index = 0

      while (index < source.length) {
        if (inComment) {
          const end = source.indexOf('*/', index)
          if (end < 0) return out + span('pl-c', source.slice(index))
          out += span('pl-c', source.slice(index, end + 2))
          index = end + 2
          inComment = false
          continue
        }

        const rest = source.slice(index)
        if (rest.startsWith('/*')) { inComment = true; index += 2; out += span('pl-c', '/*'); continue }
        if (rest.startsWith('//')) return out + span('pl-c', rest)

        if (rest[0] === '{') { inBlock = true; out += '{'; index++; continue }
        if (rest[0] === '}') { inBlock = false; out += '}'; index++; continue }

        const string = rest.match(/^("[^"]*"|'[^']*')/)
        if (string) { out += span('pl-s', string[0]); index += string[0].length; continue }

        if (!inBlock) {
          const at = rest.match(/^@[A-Za-z-]+/)
          if (at) { out += span('pl-k', at[0]); index += at[0].length; continue }
          const selector = rest.match(/^[^{};/'"]+/)
          if (selector) { out += span('pl-ent', selector[0]); index += selector[0].length; continue }
        } else {
          const property = rest.match(/^([-A-Za-z]+)(\s*:)/)
          if (property) {
            out += span('pl-c1', property[1]) + escapeHtml(property[2])
            index += property[0].length
            continue
          }
          const value = rest.match(/^(#[0-9a-fA-F]{3,8}|-?\d+(\.\d+)?[a-z%]*|\$[\w-]+|--[\w-]+)/)
          if (value) { out += span('pl-c1', value[0]); index += value[0].length; continue }
        }

        out += escapeHtml(source[index])
        index += 1
      }
      return out
    }
  }

  const TWIG_OPERATORS = /^(?:in|is|not|and|or|b-and|b-or|b-xor|if|else|elseif|as|with|only|from|import|matches|starts with|ends with|same as|divisible by)\b/
  const TWIG_CONSTANTS = /^(?:true|false|null|none)\b/

  function twigTag(open, body, close) {
    if (open === '{#') return span('pl-c', open + body + close)

    let out = span('pl-k', open)
    let index = 0
    let first = open.startsWith('{%')

    while (index < body.length) {
      const rest = body.slice(index)
      let hit
      if ((hit = rest.match(/^\s+/))) { out += escapeHtml(hit[0]); index += hit[0].length; continue }
      if ((hit = rest.match(/^("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')/))) { out += span('pl-s', hit[0]); index += hit[0].length; continue }
      if ((hit = rest.match(/^\d+(?:\.\d+)?/))) { out += span('pl-c1', hit[0]); index += hit[0].length; continue }
      if ((hit = rest.match(/^\|\s*[A-Za-z_]\w*/))) {
        out += escapeHtml('|') + span('pl-en', hit[0].slice(1))
        index += hit[0].length
        continue
      }
      if ((hit = rest.match(/^[A-Za-z_][\w-]*/))) {
        const word = hit[0]
        if (first) out += span('pl-k', word)
        else if (TWIG_CONSTANTS.test(word)) out += span('pl-c1', word)
        else if (TWIG_OPERATORS.test(word)) out += span('pl-k', word)
        else if (/^\s*\(/.test(body.slice(index + word.length))) out += span('pl-en', word)
        else out += escapeHtml(word)
        first = false
        index += word.length
        continue
      }
      out += escapeHtml(rest[0])
      index += 1
    }
    return out + span('pl-k', close)
  }

  function markup() {
    let inComment = false
    let inTag = false

    return function highlightLine(source) {
      let out = ''
      let index = 0

      while (index < source.length) {
        if (inComment) {
          const end = source.indexOf('-->', index)
          if (end < 0) return out + span('pl-c', source.slice(index))
          out += span('pl-c', source.slice(index, end + 3))
          index = end + 3
          inComment = false
          continue
        }

        const rest = source.slice(index)

        if (!inTag) {
          if (rest.startsWith('<!--')) { inComment = true; out += span('pl-c', '<!--'); index += 4; continue }
          const twig = rest.match(/^(\{\{-?|\{%-?|\{#)([\s\S]*?)(-?\}\}|-?%\}|#\})/)
          if (twig) {
            out += twigTag(twig[1], twig[2], twig[3])
            index += twig[0].length
            continue
          }
          const tag = rest.match(/^<\/?([A-Za-z][A-Za-z0-9:-]*)/)
          if (tag) {
            out += escapeHtml(tag[0].slice(0, tag[0].length - tag[1].length)) + span('pl-ent', tag[1])
            index += tag[0].length
            inTag = true
            continue
          }
          const text = rest.match(/^[^<{]+/)
          if (text) { out += escapeHtml(text[0]); index += text[0].length; continue }
          out += escapeHtml(source[index])
          index += 1
          continue
        }

        if (rest[0] === '>' || rest.startsWith('/>')) {
          const token = rest[0] === '>' ? '>' : '/>'
          out += escapeHtml(token)
          index += token.length
          inTag = false
          continue
        }
        const string = rest.match(/^("[^"]*"|'[^']*')/)
        if (string) { out += span('pl-s', string[0]); index += string[0].length; continue }
        const attribute = rest.match(/^[A-Za-z_:@#.\[\]][A-Za-z0-9_:@.\-\[\]]*/)
        if (attribute) { out += span('pl-c1', attribute[0]); index += attribute[0].length; continue }

        out += escapeHtml(source[index])
        index += 1
      }
      return out
    }
  }

  function markdownLine() {
    let inFence = false

    return function highlightLine(source) {
      if (/^\s*```/.test(source)) { inFence = !inFence; return span('pl-c', source) }
      if (inFence) return escapeHtml(source)

      const heading = source.match(/^(#{1,6}\s+)(.*)$/)
      if (heading) return span('pl-k', heading[1]) + span('pl-ent', heading[2])
      if (/^\s*>/.test(source)) return span('pl-c', source)

      let out = ''
      const re = /(`[^`]*`)|(\*\*[^*]+\*\*|__[^_]+__)|(\[[^\]]*\]\([^)\s]*\))|^(\s*(?:[-*+]|\d+\.)\s)/g
      let last = 0
      let hit
      while ((hit = re.exec(source))) {
        out += escapeHtml(source.slice(last, hit.index))
        if (hit[1]) out += span('pl-s', hit[1])
        else if (hit[2]) out += span('pl-ent', hit[2])
        else if (hit[3]) out += span('pl-en', hit[3])
        else out += span('pl-k', hit[0])
        last = hit.index + hit[0].length
      }
      return out + escapeHtml(source.slice(last))
    }
  }

  const LANGUAGES = {
    php, javascript, python, go, rust, java, c: cfamily, sql, shell,
    json, yaml, ini, css, markup, markdown: markdownLine,
  }

  function highlighterFor(language) {
    const factory = LANGUAGES[language]
    return factory ? factory() : (source) => escapeHtml(source)
  }

  function inline(text) {
    return escapeHtml(text)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>')
      .replace(/~~([^~]+)~~/g, '<del>$1</del>')
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" rel="noopener">$1</a>')
      .replace(/(^|\s)(#\d+)/g, '$1<a href="#">$2</a>')
  }

  function markdown(source) {
    const lines = String(source ?? '').split('\n')
    const html = []
    let paragraph = []
    let list = null
    let inCode = false
    let code = []

    const flushParagraph = () => {
      if (paragraph.length) {
        html.push(`<p>${inline(paragraph.join('\n')).replace(/\n/g, '<br>')}</p>`)
        paragraph = []
      }
    }
    const flushList = () => {
      if (list) {
        html.push(`<${list.tag}>${list.items.map((item) => `<li>${inline(item)}</li>`).join('')}</${list.tag}>`)
        list = null
      }
    }
    const flush = () => {
      flushParagraph()
      flushList()
    }

    for (let index = 0; index < lines.length; index++) {
      const line = lines[index]

      if (line.trim().startsWith('```')) {
        if (inCode) {
          html.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`)
          code = []
          inCode = false
        } else {
          flush()
          inCode = true
        }
        continue
      }
      if (inCode) {
        code.push(line)
        continue
      }

      if (!line.trim()) {
        flush()
        continue
      }

      const heading = line.match(/^(#{1,6})\s+(.*)$/)
      if (heading) {
        flush()
        html.push(`<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`)
        continue
      }

      if (/^([-*_])\1{2,}$/.test(line.trim())) {
        flush()
        html.push('<hr>')
        continue
      }

      if (line.trim().startsWith('>')) {
        flush()
        const quote = []
        while (index < lines.length && lines[index].trim().startsWith('>')) {
          quote.push(lines[index].replace(/^\s*>\s?/, ''))
          index++
        }
        index--
        html.push(`<blockquote>${markdown(quote.join('\n'))}</blockquote>`)
        continue
      }

      if (line.trim().startsWith('|') && lines[index + 1]?.includes('---')) {
        flush()
        const cells = (row) =>
          row.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim())
        const head = cells(line)
        index += 2
        const body = []
        while (index < lines.length && lines[index].trim().startsWith('|')) {
          body.push(cells(lines[index]))
          index++
        }
        index--
        html.push(
          `<table><thead><tr>${head.map((cell) => `<th>${inline(cell)}</th>`).join('')}</tr></thead>` +
            `<tbody>${body
              .map((row) => `<tr>${row.map((cell) => `<td>${inline(cell)}</td>`).join('')}</tr>`)
              .join('')}</tbody></table>`,
        )
        continue
      }

      const bullet = line.match(/^\s*[-*]\s+(.*)$/)
      const numbered = line.match(/^\s*\d+\.\s+(.*)$/)
      if (bullet || numbered) {
        flushParagraph()
        const tag = bullet ? 'ul' : 'ol'
        if (!list || list.tag !== tag) {
          flushList()
          list = { tag, items: [] }
        }
        list.items.push((bullet ?? numbered)[1])
        continue
      }

      flushList()
      paragraph.push(line)
    }

    if (inCode) html.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`)
    flush()

    return html.join('')
  }

  return { escapeHtml, highlighterFor, markdown }
})()
