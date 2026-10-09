import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../api";
import type { Category } from "../models";

type Props = {
  categories: Category[];
  parent: string;
  token: string;
  onClose: () => void;
  onCreated: (id: string, categories: Category[]) => void;
  onBusy: (busy: boolean) => void;
};

export function CategoryDialog({
  categories,
  parent,
  token,
  onClose,
  onCreated,
  onBusy,
}: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState("");
  const [root, setRoot] = useState(parent);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  return (
    <dialog
      ref={dialog}
      aria-labelledby="category-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (busy) return;
          setBusy(true);
          onBusy(true);
          setError("");
          void api
            .createCategory(name.trim(), root, token)
            .then((result) => onCreated(result.id, result.categories))
            .catch((error) => setError(errorMessage(error)))
            .finally(() => {
              setBusy(false);
              onBusy(false);
            });
        }}
      >
        <div className="dialog-heading">
          <div className="eyebrow">MAKE IT YOURS</div>
          <h2 id="category-title">添加一个生活分类</h2>
          <p>可以是新的大类，也可以更具体一点。</p>
        </div>
        <fieldset disabled={busy}>
          <label htmlFor="category-parent">所属大类</label>
          <select
            id="category-parent"
            value={root}
            onChange={(event) => setRoot(event.target.value)}
          >
            <option value="">新的大类（无上级）</option>
            {categories
              .filter((item) => !item.parent_id)
              .map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
          </select>
          <label htmlFor="category-name">分类名称</label>
          <input
            id="category-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            required
            maxLength={40}
            placeholder="例如：攀岩、游泳、音乐剧"
            autoComplete="off"
            autoFocus
          />
          <p className="field-help">
            不设置“其他”；不确定的消费可以先留待核对。
          </p>
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
          <div className="dialog-actions">
            <button
              type="button"
              className="secondary-button"
              onClick={onClose}
            >
              取消
            </button>
            <button type="submit" className="primary-button">
              {busy ? "正在添加…" : "添加分类"}
            </button>
          </div>
        </fieldset>
      </form>
    </dialog>
  );
}

export function CategoryManager({
  categories,
  onAdd,
}: {
  categories: Category[];
  onAdd: (parent: string) => void;
}) {
  return (
    <section id="categories-view">
      <div className="view-toolbar">
        <div>
          <h2>生活的分类，由你定义</h2>
          <p>支持大类与一级小类，没有“其他”。添加分类不会自动修改账单。</p>
        </div>
        <button className="primary-button" onClick={() => onAdd("")}>
          ＋ 添加大类
        </button>
      </div>
      <div className="category-grid">
        {categories
          .filter((item) => !item.parent_id)
          .map((root) => {
            const children = categories.filter(
              (item) => item.parent_id === root.id,
            );
            return (
              <article className="category-card" key={root.id}>
                <h3>{root.name}</h3>
                <div className="category-children">
                  {children.length ? (
                    children.map((child) => (
                      <span className="category-child" key={child.id}>
                        {child.name}
                      </span>
                    ))
                  ) : (
                    <p>还没有细分小类</p>
                  )}
                </div>
                <button className="text-button" onClick={() => onAdd(root.id)}>
                  ＋ 添加小类
                </button>
              </article>
            );
          })}
      </div>
    </section>
  );
}
